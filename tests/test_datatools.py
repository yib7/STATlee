"""Dataset ingestion & profiling: format normalization (5.1) and the
metadata-only prompt summaries (5.10)."""
import os

import pandas as pd
import pytest

from statlee import datatools


def _write_csv(tmp_path, name='d.csv'):
    p = tmp_path / name
    pd.DataFrame({
        'age': [25, 40, None, 31],
        'income': [30000, 55000, 42000, None],
        'group': ['A', 'B', 'A', 'B'],
    }).to_csv(p, index=False)
    return str(p)


def test_supported_extensions_cover_requested_formats():
    for ext in ('.csv', '.tsv', '.xlsx', '.xls', '.dta', '.sav'):
        assert ext in datatools.SUPPORTED_EXTENSIONS


def test_normalize_csv_is_passthrough(tmp_path):
    src = _write_csv(tmp_path)
    out, labels = datatools.normalize_to_csv(src)
    assert out == src               # CSV needs no conversion
    assert labels == {}


def test_normalize_tsv_to_csv(tmp_path):
    src = tmp_path / 'd.tsv'
    pd.DataFrame({'a': [1, 2], 'b': [3, 4]}).to_csv(src, sep='\t', index=False)
    out, labels = datatools.normalize_to_csv(str(src))
    assert out.endswith('.csv')
    assert os.path.exists(out)
    assert not os.path.exists(src)  # source removed; one canonical artifact
    df = pd.read_csv(out)
    assert list(df.columns) == ['a', 'b']


def test_normalize_unsupported_raises(tmp_path):
    src = tmp_path / 'd.parquet'
    src.write_text('not really parquet')
    with pytest.raises(datatools.UnsupportedFormatError):
        datatools.normalize_to_csv(str(src))


def test_normalize_xls_missing_engine_names_xlrd(tmp_path, monkeypatch):
    """.xls is read via the xlrd engine (openpyxl only covers .xlsx), so the
    remediation message must name xlrd, not openpyxl."""
    src = tmp_path / 'd.xls'
    src.write_bytes(b'not really an xls')

    def _raise(*args, **kwargs):
        raise ImportError("Missing optional dependency 'xlrd'.")

    monkeypatch.setattr(pd, 'read_excel', _raise)
    with pytest.raises(datatools.MissingDependencyError) as exc_info:
        datatools.normalize_to_csv(str(src))
    assert 'xlrd' in str(exc_info.value)


def test_normalize_xlsx_missing_engine_names_openpyxl(tmp_path, monkeypatch):
    src = tmp_path / 'd.xlsx'
    src.write_bytes(b'not really an xlsx')

    def _raise(*args, **kwargs):
        raise ImportError("Missing optional dependency 'openpyxl'.")

    monkeypatch.setattr(pd, 'read_excel', _raise)
    with pytest.raises(datatools.MissingDependencyError) as exc_info:
        datatools.normalize_to_csv(str(src))
    assert 'openpyxl' in str(exc_info.value)


# --- P2-2: decompression-bomb / oversized-parse bounds ----------------------

def test_normalize_post_read_cell_guard_rejects(tmp_path):
    """A dataset whose rows*cols exceeds max_cells raises ParseLimitError from
    the post-read backstop. Uses TSV (a real parse); CSV is a passthrough."""
    src = tmp_path / 'big.tsv'
    pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]}).to_csv(
        src, sep='\t', index=False)   # 3x2 = 6 cells
    with pytest.raises(datatools.ParseLimitError) as exc_info:
        datatools.normalize_to_csv(str(src), max_cells=2)
    assert 'too large' in str(exc_info.value).lower()
    assert src.exists()   # rejected before the source is consumed/removed


def test_normalize_cell_guard_generous_limit_succeeds(tmp_path):
    """A generous max_cells leaves a normal parse unchanged."""
    src = tmp_path / 'ok.tsv'
    pd.DataFrame({'a': [1, 2], 'b': [3, 4]}).to_csv(src, sep='\t', index=False)
    out, labels = datatools.normalize_to_csv(str(src), max_cells=1_000_000)
    assert out.endswith('.csv')
    assert os.path.exists(out)


def test_normalize_no_limits_is_unaffected(tmp_path):
    """Passing no limit args (falsy) skips every check — existing callers are
    untouched even for a frame that would blow a tiny cap."""
    src = tmp_path / 'plain.tsv'
    pd.DataFrame({'a': list(range(50)), 'b': list(range(50))}).to_csv(
        src, sep='\t', index=False)   # 100 cells
    out, _ = datatools.normalize_to_csv(str(src))   # no max_cells at all
    assert os.path.exists(out)


def test_normalize_csv_passthrough_ignores_cell_cap(tmp_path):
    """A .csv is a passthrough (not materialized here), so even a tiny cell cap
    can't reject it — it stays the canonical artifact untouched."""
    src = _write_csv(tmp_path)
    out, labels = datatools.normalize_to_csv(str(src), max_cells=2)
    assert out == src
    assert labels == {}


def test_normalize_xlsx_zip_bomb_guard_rejects(tmp_path):
    """The .xlsx zip guard rejects on the central-directory uncompressed total
    BEFORE a normal read, so an absurdly low cap trips even a tiny workbook."""
    pytest.importorskip('openpyxl')
    src = tmp_path / 'book.xlsx'
    pd.DataFrame({'a': [1, 2], 'b': [3, 4]}).to_excel(str(src), index=False)
    with pytest.raises(datatools.ParseLimitError) as exc_info:
        datatools.normalize_to_csv(str(src), max_uncompressed_bytes=10)
    assert 'too large' in str(exc_info.value).lower()
    assert src.exists()   # source untouched — rejected before read


def test_normalize_xlsx_generous_uncompressed_cap_reads(tmp_path):
    """With a generous uncompressed cap the same .xlsx reads normally."""
    pytest.importorskip('openpyxl')
    src = tmp_path / 'book.xlsx'
    pd.DataFrame({'a': [1, 2], 'b': [3, 4]}).to_excel(str(src), index=False)
    out, labels = datatools.normalize_to_csv(
        str(src), max_uncompressed_bytes=512 * 1024 * 1024, max_cells=1_000_000)
    assert out.endswith('.csv')
    df = pd.read_csv(out)
    assert list(df.columns) == ['a', 'b']


def test_file_sha256_is_stable(tmp_path):
    src = _write_csv(tmp_path)
    assert datatools.file_sha256(src) == datatools.file_sha256(src)


def test_summarize_dataframe_is_structural_not_raw():
    df = pd.DataFrame({
        'age': [25, 40, None, 31],
        'group': ['A', 'B', 'A', 'B'],
    })
    summary = datatools.summarize_dataframe(df)
    assert summary['age']['dtype'].startswith('float')
    assert summary['age']['missing'] == 1
    assert summary['age']['missing_pct'] == 25.0
    assert 'min' in summary['age'] and 'max' in summary['age']
    assert summary['group']['n_unique'] == 2
    # At most 3 sample values — never the whole column.
    assert len(summary['group']['samples']) <= 3


def test_summarize_truncates_wide_frames():
    df = pd.DataFrame({f'c{i}': [1, 2] for i in range(90)})
    summary = datatools.summarize_dataframe(df, max_columns=80)
    assert '__truncated__' in summary


def test_build_column_context_includes_codebook_and_description():
    df = pd.DataFrame({'income': [1, 2, 3]})
    ctx = datatools.build_column_context(
        df, codebook={'income': 'Continuous'},
        descriptions={'income': 'Household income bracket'})
    assert "'income'" in ctx
    assert 'Continuous' in ctx
    assert 'Household income bracket' in ctx
