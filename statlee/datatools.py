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


def normalize_to_csv(src_path, *, max_cells=None, max_uncompressed_bytes=None):
    """Read any supported format and write a CSV next to it.

    Returns ``(csv_path, labels)`` where ``labels`` maps column names to
    human-readable variable labels when the source format carries them
    (.sav/.dta); empty dict otherwise. The source file is removed once the
    CSV exists (one canonical artifact per dataset).

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

    if ext == '.csv':
        # Passthrough: no DataFrame is built and the file is not compressed, so
        # it is already bounded by the request-body size cap. Nothing to guard.
        return src_path, labels

    if ext == '.tsv':
        df = pd.read_csv(src_path, sep='\t')
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
            df = pd.read_excel(src_path)
        except ImportError as e:
            raise MissingDependencyError(
                f"Excel support requires the '{engine_pkg}' package on the "
                "server. Please upload a CSV instead, or ask the administrator "
                f"to install {engine_pkg}.") from e
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
        if max_cells:
            _, meta = reader(src_path, metadataonly=True)
            n_rows = getattr(meta, 'number_rows', 0) or 0
            n_cols = getattr(meta, 'number_columns', 0) or 0
            if n_rows * n_cols > max_cells:
                raise ParseLimitError(
                    _cell_limit_message(n_rows * n_cols, max_cells))
        df, meta = reader(src_path)
        # Native variable labels seed the codebook (no LLM call needed).
        names = list(getattr(meta, 'column_names', []) or [])
        col_labels = list(getattr(meta, 'column_labels', []) or [])
        labels = {name: label
                  for name, label in zip(names, col_labels, strict=False) if label}
    else:
        raise UnsupportedFormatError(
            f"Unsupported file format '{ext}'. Supported: "
            + ", ".join(SUPPORTED_EXTENSIONS))

    # Final backstop for every format that produced a DataFrame (TSV, .xls, and
    # anything that slipped the pre-checks): bound total cells before writing.
    if max_cells and df.shape[0] * df.shape[1] > max_cells:
        raise ParseLimitError(
            _cell_limit_message(df.shape[0] * df.shape[1], max_cells))

    csv_path = os.path.splitext(src_path)[0] + '.csv'
    df.to_csv(csv_path, index=False)
    try:
        os.remove(src_path)
    except OSError:
        pass
    return csv_path, labels


def profile_dataframe(df):
    return {
        'total_columns': len(df.columns),
        'numeric_columns': df.select_dtypes(include=['number']).columns.tolist(),
        'categorical_columns': df.select_dtypes(exclude=['number']).columns.tolist(),
        'headers': df.columns.tolist(),
    }


def summarize_dataframe(df, max_columns=80):
    """Per-column structural metadata for LLM prompts (5.10).

    Compact on purpose: dtype, measurement hints, missingness, cardinality,
    numeric range, and at most 3 sample values. No raw rows.
    """
    summary = {}
    for col in df.columns[:max_columns]:
        series = df[col]
        entry = {
            'dtype': str(series.dtype),
            'n_unique': int(series.nunique(dropna=True)),
            'missing': int(series.isna().sum()),
        }
        if len(df) > 0:
            entry['missing_pct'] = round(100.0 * entry['missing'] / len(df), 1)
        if pd.api.types.is_numeric_dtype(series) and series.notna().any():
            desc = series.describe()
            entry['min'] = round(float(desc.get('min', float('nan'))), 4)
            entry['median'] = round(float(series.median()), 4)
            entry['max'] = round(float(desc.get('max', float('nan'))), 4)
        entry['samples'] = series.dropna().astype(str).unique()[:3].tolist()
        summary[col] = entry
    if len(df.columns) > max_columns:
        summary['__truncated__'] = (
            f"{len(df.columns) - max_columns} additional columns omitted")
    return summary


def metadata_json(df, max_columns=80):
    return json.dumps(summarize_dataframe(df, max_columns), indent=1, default=str)


def build_column_context(df, codebook=None, descriptions=None):
    """Rich per-column context lines for /suggest and /method_prompt.

    Layers: codebook classification, description (PDF/survey/native label),
    and the structural metadata summary.
    """
    codebook = codebook or {}
    desc_lower = {k.lower(): v for k, v in (descriptions or {}).items()}
    summary = summarize_dataframe(df)
    lines = []
    for col in df.columns:
        meta = summary.get(col, {})
        classification = codebook.get(col, 'Unknown')
        parts = [f"dtype={meta.get('dtype')}",
                 f"unique={meta.get('n_unique')}",
                 f"missing={meta.get('missing')}"]
        if 'min' in meta:
            parts.append(f"range=[{meta['min']}..{meta['max']}], median={meta['median']}")
        line = f"  - '{col}' [{classification}; {', '.join(parts)}; samples={meta.get('samples')}]"
        description = desc_lower.get(col.lower(), '')
        if description:
            line += f"\n      Codebook description: {description}"
        lines.append(line)
    return "\n".join(lines)
