"""Dataset reading and profiling utilities.

- 5.1  Multi-format ingestion: CSV/TSV/Excel/Stata/SPSS, normalized to CSV
       internally so generated analysis code can always ``read_csv``.
       Native variable labels from .sav/.dta seed the codebook for free.
- 5.10 Metadata-driven prompt context: compact per-column structural
       summaries (dtype, uniques, missingness, numeric range) instead of
       raw row dumps.
"""
import hashlib
import json
import os
import re
import zipfile

import pandas as pd

SUPPORTED_EXTENSIONS = ('.csv', '.tsv', '.xlsx', '.xls', '.dta', '.sav')


class UnsupportedFormatError(ValueError):
    pass


class MissingDependencyError(RuntimeError):
    pass


class ParseLimitError(ValueError):
    """An upload that parses within request-body limits but would still expand
    to too many cells / too much decompressed data to materialize safely in the
    web worker (P2-2). The route maps this to a 413."""
    pass


class DatasetParseError(ValueError):
    """The upload is a supported format but its content cannot be parsed (empty
    file, ragged rows, ...). The message is user-facing; the route maps it to
    a 422 (D1-8)."""
    pass


def decode_text_bytes(raw):
    """Decode uploaded text: UTF-8 (BOM tolerated) first, then cp1252 (what
    Excel on Windows and many SPSS exports write), then latin-1, which cannot
    fail. Returns ``(text, encoding)``. A UTF-16 byte-order mark (Excel's
    "Unicode Text" export) is honoured first."""
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            return raw.decode('utf-16'), 'utf-16'
        except UnicodeDecodeError:
            pass
    for enc in ('utf-8-sig', 'cp1252'):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1'), 'latin-1'


def _transcode_to_utf8(path):
    """Rewrite a non-UTF-8 text file in place as UTF-8 so every later reader
    (pandas, the generated scripts, R) sees one encoding. Returns the source
    encoding."""
    with open(path, 'rb') as f:
        raw = f.read()
    text, enc = decode_text_bytes(raw)
    if enc != 'utf-8-sig':
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
    return enc


def _read_delimited(path, sep=','):
    """Full parse of a CSV/TSV with clear, user-facing failures (D1-8). The
    whole file is parsed, not a 100-row preview, so a ragged row deep in the
    file is caught at upload instead of breaking /data_page later."""
    _refuse_binary(path)
    _transcode_to_utf8(path)
    try:
        df = pd.read_csv(path, sep=sep)
    except pd.errors.EmptyDataError as e:
        raise DatasetParseError(
            'The file is empty. Upload a file with a header row and data.') from e
    except pd.errors.ParserError as e:
        m = re.search(r'line (\d+), saw (\d+)', str(e))
        where = (f' Line {m.group(1)} has {m.group(2)} fields, unlike the '
                 'header.') if m else ''
        raise DatasetParseError(
            'Could not parse the file: the rows do not all have the same '
            f'number of columns.{where} Check the delimiter and quoting, then '
            'upload again.') from e
    return df


_BINARY_SNIFF_BYTES = 64 * 1024
# Control bytes that never appear in a text CSV (everything below 0x20 except
# tab, newline, form feed and carriage return).
_CONTROL_BYTES = frozenset(range(32)) - {9, 10, 12, 13}


def _refuse_binary(path):
    """A file named .csv/.tsv that holds binary data (an Excel workbook
    renamed, a corrupt download) has NUL bytes or a run of control bytes in
    its first block; text has neither, except UTF-16, which starts with a
    byte-order mark. Refusing it here gives an actionable message instead of
    a ragged-rows error or a one-column dataset of garbage."""
    with open(path, 'rb') as f:
        head = f.read(_BINARY_SNIFF_BYTES)
    if not head or head.startswith((b'\xff\xfe', b'\xfe\xff')):
        return
    control = sum(1 for byte in head if byte in _CONTROL_BYTES)
    if b'\x00' in head or control > len(head) // 100:
        raise DatasetParseError(
            'This file is not plain text, so it cannot be read as a CSV. If '
            'it is an Excel, Stata or SPSS file, upload it with its real '
            'extension (.xlsx, .xls, .dta or .sav); otherwise export it as '
            'CSV and upload again.')


# Format names and the program that writes them, for _damaged().
_FORMAT_NAMES = {
    '.xlsx': ('Excel workbook (.xlsx)', 'Excel'),
    '.xls': ('Excel 97-2003 workbook (.xls)', 'Excel'),
    '.dta': ('Stata file (.dta)', 'Stata'),
    '.sav': ('SPSS file (.sav)', 'SPSS'),
}


def _damaged(ext):
    name, program = _FORMAT_NAMES[ext]
    return DatasetParseError(
        f'Could not read this {name}: it looks damaged, or it is not really '
        f'that format. Open it in {program} (or the program that made it) and '
        'save it again, or export it as CSV, then upload again.')


def _no_rows_error():
    return DatasetParseError(
        'The file has column names but no data rows. Upload a file with at '
        'least one row of data.')


_VALUE_LABELS_MAX = 40


def _fmt_code(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _merge_value_labels(names, col_labels, value_labels):
    """Native .sav/.dta labels for the codebook: the variable label plus its
    value labels, e.g. ``"Respondent gender (1=Male, 2=Female)"`` (D1-13).
    Without the value labels the model saw a coded column as continuous."""
    labels = {}
    for name, label in zip(names, col_labels, strict=False):
        if label:
            labels[name] = str(label)
    for name, mapping in (value_labels or {}).items():
        if not isinstance(mapping, dict) or not mapping:
            continue
        pairs = [f'{_fmt_code(k)}={v}' for k, v in
                 list(mapping.items())[:_VALUE_LABELS_MAX]]
        if len(mapping) > _VALUE_LABELS_MAX:
            pairs.append('...')
        coding = ', '.join(pairs)
        base = labels.get(name)
        labels[name] = f'{base} ({coding})' if base else coding
    return labels


def _missing_codes(ranges):
    """User-missing values of one .sav column (``[{'lo', 'hi'}]``) as codes:
    a discrete value is its code, a range reads ``"98 to 99"``."""
    codes = []
    for item in ranges or []:
        if not isinstance(item, dict):
            continue
        lo, hi = item.get('lo'), item.get('hi')
        if lo is None:
            continue
        code = _fmt_code(lo) if hi is None or hi == lo \
            else f'{_fmt_code(lo)} to {_fmt_code(hi)}'
        if code not in codes:
            codes.append(code)
    return codes


def native_codebook(names, col_labels, value_labels, missing_ranges=None):
    """Structured native .sav/.dta metadata for the server codebook (SP2):
    ``{column: {label, value_labels: {code: text}, missing_codes: [...]}}``,
    only the fields the file actually carries."""
    out = {}
    for name, label in zip(names, col_labels, strict=False):
        if label:
            out.setdefault(str(name), {})['label'] = str(label)
    for name, mapping in (value_labels or {}).items():
        if isinstance(mapping, dict) and mapping:
            out.setdefault(str(name), {})['value_labels'] = {
                _fmt_code(k): str(v) for k, v in mapping.items()}
    for name, ranges in (missing_ranges or {}).items():
        codes = _missing_codes(ranges)
        if codes:
            out.setdefault(str(name), {})['missing_codes'] = codes
    return out


def _read_excel_all_sheets(src_path, info):
    """Read every sheet and keep the one with the most cells (D1-13). A
    workbook whose first sheet is a README/notes tab used to be ingested as
    "the dataset"; now the choice is made on content and announced."""
    sheets = pd.read_excel(src_path, sheet_name=None)
    if not sheets:
        raise DatasetParseError('The workbook has no sheets.')
    names = list(sheets)
    chosen = max(names, key=lambda n: (sheets[n].size, -names.index(n)))
    if len(names) > 1:
        others = ', '.join(f"'{n}'" for n in names if n != chosen)
        info.setdefault('warnings', []).append(
            f"This workbook has {len(names)} sheets. Imported '{chosen}' (the "
            f"largest); ignored {others}. Save the sheet you want as its own "
            'file to analyze a different one.')
    return sheets[chosen]


def _cell_limit_message(n_cells, cap):
    return (
        f"This file is too large to process: it expands to ~{n_cells:,} cells, "
        f"above the {cap:,}-cell limit. Please upload a smaller extract or a "
        "CSV.")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def normalize_to_csv(src_path, *, max_cells=None, max_uncompressed_bytes=None,
                     info=None):
    """Read any supported format and write a CSV next to it.

    ``info``, when given, is a dict the function fills with ``warnings`` (a
    list of user-facing notes, e.g. skipped workbook sheets) and ``frame``
    (the parsed DataFrame, so the caller need not parse the CSV again).

    Returns ``(csv_path, labels)`` where ``labels`` maps column names to
    human-readable variable labels when the source format carries them
    (.sav/.dta); empty dict otherwise. The source file is removed once the
    CSV exists (one canonical artifact per dataset). For .sav/.dta, ``info``
    also gets ``native_codebook`` (``native_codebook()``), which seeds the
    server codebook's native layer (SP2).

    ``max_cells`` and ``max_uncompressed_bytes`` bound the parse against a
    decompression bomb (P2-2): a small compressed request body can still expand
    to a DataFrame of gigabytes in the web worker. When a limit is ``None`` or
    otherwise falsy that check is skipped, so direct callers that pass nothing
    are unaffected. A dataset that would exceed a limit raises
    ``ParseLimitError`` — ideally BEFORE full materialization (zip central
    directory for .xlsx, reader metadata for .sav/.dta), with a post-read cell
    count as the final backstop for anything that slipped the pre-checks.
    """
    ext = os.path.splitext(src_path)[1].lower()
    labels = {}
    if info is None:
        info = {}
    info.setdefault('warnings', [])
    try:
        empty = os.path.getsize(src_path) == 0
    except OSError:
        empty = False
    if empty and ext in SUPPORTED_EXTENSIONS:
        raise DatasetParseError(
            'The file is empty. Upload a file with a header row and data.')

    if ext == '.csv':
        # Kept in place (an uncompressed CSV is already bounded by the request
        # body cap, so no cell guard), but transcoded to UTF-8 if needed and
        # fully parsed so an unreadable file fails HERE with a clear 422
        # instead of a 500 later (D1-8).
        df = _read_delimited(src_path)
        if df.empty and len(df.columns):
            raise _no_rows_error()
        info['frame'] = df
        return src_path, labels

    if ext == '.tsv':
        df = _read_delimited(src_path, sep='\t')
    elif ext in ('.xlsx', '.xls'):
        engine_pkg = 'openpyxl' if ext == '.xlsx' else 'xlrd'
        # .xlsx is a zip container: a 16 MB upload can hold cells that unpack to
        # gigabytes. Sum the central-directory uncompressed sizes (this reads
        # metadata only — it does NOT extract) and reject before pd.read_excel.
        # .xls is a legacy compound binary (inherently capped near 65k rows),
        # not a zip, so the post-read cell guard below covers it.
        if max_uncompressed_bytes and zipfile.is_zipfile(src_path):
            with zipfile.ZipFile(src_path) as zf:
                total = sum(zi.file_size for zi in zf.infolist())
            if total > max_uncompressed_bytes:
                raise ParseLimitError(
                    "This file is too large to process: it unpacks to "
                    f"~{total // (1024 * 1024)} MB, above the "
                    f"{max_uncompressed_bytes // (1024 * 1024)} MB "
                    "uncompressed-size limit. Please upload a smaller extract "
                    "or a CSV.")
        try:
            df = _read_excel_all_sheets(src_path, info)
        except ImportError as e:
            raise MissingDependencyError(
                f"Excel support requires the '{engine_pkg}' package on the "
                "server. Please upload a CSV instead, or ask the administrator "
                f"to install {engine_pkg}.") from e
        except DatasetParseError:
            raise
        except Exception as e:
            raise _damaged(ext) from e
    elif ext in ('.dta', '.sav'):
        try:
            import pyreadstat
        except ImportError as e:
            raise MissingDependencyError(
                f"{ext} support requires the 'pyreadstat' package on the "
                "server. Please upload a CSV instead, or ask the "
                "administrator to install pyreadstat.") from e
        reader = pyreadstat.read_sav if ext == '.sav' else pyreadstat.read_dta
        # Read metadata only (no data rows) first, so an oversized dataset is
        # rejected before every cell is materialized into memory.
        try:
            if max_cells:
                _, meta = reader(src_path, metadataonly=True)
                n_rows = getattr(meta, 'number_rows', 0) or 0
                n_cols = getattr(meta, 'number_columns', 0) or 0
                if n_rows * n_cols > max_cells:
                    raise ParseLimitError(
                        _cell_limit_message(n_rows * n_cols, max_cells))
            df, meta = reader(src_path)
        except ParseLimitError:
            raise
        except Exception as e:
            # pyreadstat raises its own ReadstatError for a truncated or
            # mislabelled file.
            raise _damaged(ext) from e
        # Native variable AND value labels seed the codebook (no LLM call).
        names = list(getattr(meta, 'column_names', []) or [])
        col_labels = list(getattr(meta, 'column_labels', []) or [])
        value_labels = getattr(meta, 'variable_value_labels', None) or {}
        labels = _merge_value_labels(names, col_labels, value_labels)
        # SP2: user-missing values are read as NaN; their codes are only in
        # the metadata of a user_missing read (metadata only, no data rows).
        missing = {}
        try:
            _, missing_meta = reader(src_path, metadataonly=True,
                                     user_missing=True)
            missing = getattr(missing_meta, 'missing_ranges', None) or {}
        except Exception:
            missing = {}
        info['native_codebook'] = native_codebook(
            names, col_labels, value_labels, missing)
    else:
        raise UnsupportedFormatError(
            f"Unsupported file format '{ext}'. Supported: "
            + ", ".join(SUPPORTED_EXTENSIONS))

    # Final backstop for every format that produced a DataFrame (TSV, .xls, and
    # anything that slipped the pre-checks): bound total cells before writing.
    if max_cells and df.shape[0] * df.shape[1] > max_cells:
        raise ParseLimitError(
            _cell_limit_message(df.shape[0] * df.shape[1], max_cells))
    if df.empty:
        if len(df.columns):
            raise _no_rows_error()
        raise DatasetParseError(
            'The file has no data. Upload a file with a header row and data.')

    csv_path = os.path.splitext(src_path)[0] + '.csv'
    df.to_csv(csv_path, index=False)
    try:
        os.remove(src_path)
    except OSError:
        pass
    info['frame'] = df
    return csv_path, labels


def profile_dataframe(df):
    return {
        'total_columns': len(df.columns),
        'numeric_columns': df.select_dtypes(include=['number']).columns.tolist(),
        'categorical_columns': df.select_dtypes(exclude=['number']).columns.tolist(),
        'headers': df.columns.tolist(),
    }


_SAMPLE_SCAN_ROWS = 200


def _column_summary(series):
    """One column's entry of ``summarize_dataframe``."""
    n_rows = len(series)
    entry = {
        'dtype': str(series.dtype),
        'n_unique': int(series.nunique(dropna=True)),
        'missing': int(series.isna().sum()),
    }
    if n_rows > 0:
        entry['missing_pct'] = round(100.0 * entry['missing'] / n_rows, 1)
    if pd.api.types.is_numeric_dtype(series) and series.notna().any():
        desc = series.describe()
        entry['min'] = round(float(desc.get('min', float('nan'))), 4)
        entry['median'] = round(float(series.median()), 4)
        entry['max'] = round(float(desc.get('max', float('nan'))), 4)
    entry['samples'] = (series.head(_SAMPLE_SCAN_ROWS).dropna().astype(str)
                        .unique()[:3].tolist())
    return entry


def summarize_dataframe(df, max_columns=80):
    """Per-column structural metadata for LLM prompts (5.10).

    Compact on purpose: dtype, measurement hints, missingness, cardinality,
    numeric range, and at most 3 sample values. No raw rows.

    D3-7: pass the FULL frame. Every statistic describes all rows (callers used
    to pass the first 100 rows, so a column missing only after row 100 was
    reported as complete), the true size is in ``__dataset__``, and only the
    sample values come from the first rows.
    """
    summary = {'__dataset__': {'rows': int(len(df)),
                               'columns': int(len(df.columns))}}
    for col in df.columns[:max_columns]:
        summary[col] = _column_summary(df[col])
    if len(df.columns) > max_columns:
        summary['__truncated__'] = (
            f"{len(df.columns) - max_columns} additional columns omitted")
    return summary


def metadata_json(df, max_columns=80):
    return json.dumps(summarize_dataframe(df, max_columns), indent=1, default=str)


# The name-only tail ``build_column_context`` appends past its budget.
NAME_ONLY_TAIL_MAX = 8000


def build_column_context(df, codebook=None, descriptions=None, max_chars=None):
    """Rich per-column context lines for /suggest and /method_prompt.

    Layers: codebook classification, description (PDF/survey/native label),
    and the structural metadata summary.

    With ``max_chars``, columns are described in full, in order, while the
    text stays within that budget; the remaining columns are then listed by
    name only (at most ``NAME_ONLY_TAIL_MAX`` more characters), so a wide
    survey neither bills its whole profile on every prompt nor hides that
    the later columns exist. Columns past the budget are not profiled.
    """
    codebook = codebook or {}
    desc_lower = {str(k).lower(): v for k, v in (descriptions or {}).items()}
    lines = [f"  (dataset: {len(df):,} rows x {len(df.columns)} columns; "
             "statistics cover every row, samples are from the first rows)"]
    used = len(lines[0])
    columns = list(df.columns)
    for i, col in enumerate(columns):
        meta = _column_summary(df.iloc[:, i])
        classification = codebook.get(col, 'Unknown')
        parts = [f"dtype={meta.get('dtype')}",
                 f"unique={meta.get('n_unique')}",
                 f"missing={meta.get('missing')}"]
        if 'min' in meta:
            parts.append(f"range=[{meta['min']}..{meta['max']}], median={meta['median']}")
        line = f"  - '{col}' [{classification}; {', '.join(parts)}; samples={meta.get('samples')}]"
        description = desc_lower.get(str(col).lower(), '')
        if description:
            line += f"\n      Codebook description: {description}"
        if max_chars is not None and used + 1 + len(line) > max_chars:
            lines.append(_name_only_tail(columns[i:]))
            break
        lines.append(line)
        used += 1 + len(line)
    return "\n".join(lines)


def _name_only_tail(columns):
    """``  (N more columns, names only: 'a', 'b', ...)`` within
    ``NAME_ONLY_TAIL_MAX`` characters."""
    head = f"  ({len(columns)} more columns, names only: "
    names, size = [], len(head)
    for col in columns:
        quoted = repr(str(col))
        if size + len(quoted) + 2 > NAME_ONLY_TAIL_MAX:
            break
        names.append(quoted)
        size += len(quoted) + 2
    rest = len(columns) - len(names)
    tail = ', '.join(names)
    if rest:
        tail += f"{', ' if names else ''}and {rest} more"
    return head + tail + ")"
