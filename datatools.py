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

import pandas as pd

SUPPORTED_EXTENSIONS = ('.csv', '.tsv', '.xlsx', '.xls', '.dta', '.sav')


class UnsupportedFormatError(ValueError):
    pass


class MissingDependencyError(RuntimeError):
    pass


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def normalize_to_csv(src_path):
    """Read any supported format and write a CSV next to it.

    Returns ``(csv_path, labels)`` where ``labels`` maps column names to
    human-readable variable labels when the source format carries them
    (.sav/.dta); empty dict otherwise. The source file is removed once the
    CSV exists (one canonical artifact per dataset).
    """
    ext = os.path.splitext(src_path)[1].lower()
    labels = {}

    if ext == '.csv':
        return src_path, labels

    if ext == '.tsv':
        df = pd.read_csv(src_path, sep='\t')
    elif ext in ('.xlsx', '.xls'):
        try:
            df = pd.read_excel(src_path)
        except ImportError as e:
            raise MissingDependencyError(
                "Excel support requires the 'openpyxl' package on the server. "
                "Please upload a CSV instead, or ask the administrator to "
                "install openpyxl.") from e
    elif ext in ('.dta', '.sav'):
        try:
            import pyreadstat
        except ImportError as e:
            raise MissingDependencyError(
                f"{ext} support requires the 'pyreadstat' package on the "
                "server. Please upload a CSV instead, or ask the "
                "administrator to install pyreadstat.") from e
        reader = pyreadstat.read_sav if ext == '.sav' else pyreadstat.read_dta
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
