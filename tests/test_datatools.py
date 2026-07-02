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
