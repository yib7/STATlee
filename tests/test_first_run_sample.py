"""First run (6.12): a new user with no dataset sees guidance and can download
the synthetic sample survey the README points at."""
import pathlib

from statlee.routes import misc

SAMPLE = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'examples' / 'sample_survey.csv'


def test_empty_workspace_offers_the_sample(client):
    html = client.get('/').get_data(as_text=True)
    assert 'Drop your dataset here' in html
    assert 'href="/sample_survey.csv"' in html
    assert 'Download the sample survey' in html


def test_sample_downloads_as_the_committed_csv(client, fake_llm):
    resp = client.get('/sample_survey.csv')
    assert resp.status_code == 200
    assert resp.mimetype == 'text/csv'
    assert 'attachment' in resp.headers['Content-Disposition']
    assert resp.data == SAMPLE.read_bytes()
    assert not fake_llm.calls


def test_no_sample_no_link(client, monkeypatch, tmp_path):
    """An install without docs/ (a wheel) shows no dead link and 404s."""
    monkeypatch.setattr(misc, 'SAMPLE_DATASET', str(tmp_path / 'missing.csv'))
    assert 'sample_survey.csv' not in client.get('/').get_data(as_text=True)
    assert client.get('/sample_survey.csv').status_code == 404
