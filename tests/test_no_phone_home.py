"""Pages load nothing from a third-party host (Phase 4, 4.8).

The three pages used to pull Inter, Syne and JetBrains Mono from Google
Fonts, which sent every visitor's IP address to Google on each page view
without a word in the docs. The fonts are now served from static/fonts and
the CSP allows fonts and styles from this origin only.
"""
import re
from pathlib import Path

import pytest
from conftest import csrf_token

FONTS = Path(__file__).resolve().parent.parent / 'statlee' / 'static' / 'fonts'
# Anything a browser fetches on its own while rendering the page.
FETCHING_ATTR = re.compile(
    r'<(?:link|script|img|iframe|source|video|audio)\b[^>]*?'
    r'\b(?:href|src)\s*=\s*["\'](?P<url>[^"\']+)', re.IGNORECASE)


def _pages(client):
    pages = {'/': client.get('/'), '/welcome': client.get('/welcome'),
             '/reset_password': client.get('/reset_password?token=x')}
    for path, resp in pages.items():
        assert resp.status_code in (200, 400), path
    return {path: resp.get_data(as_text=True) for path, resp in pages.items()}


def test_pages_fetch_nothing_from_another_host(client):
    for path, html in _pages(client).items():
        for match in FETCHING_ATTR.finditer(html):
            url = match.group('url')
            assert not re.match(r'^(https?:)?//', url, re.IGNORECASE), (
                f'{path} makes the browser fetch {url}')
        assert 'fonts.googleapis.com' not in html
        assert 'fonts.gstatic.com' not in html


def test_every_page_loads_the_self_hosted_fonts(client):
    for path, html in _pages(client).items():
        assert '/static/fonts/fonts.css' in html, path


def test_csp_allows_fonts_and_styles_from_this_origin_only(client):
    csp = client.get('/').headers['Content-Security-Policy']
    directives = dict(part.strip().split(' ', 1)
                      for part in csp.split(';') if part.strip())
    assert directives['font-src'] == "'self'"
    assert 'http' not in directives['style-src']
    assert 'googleapis' not in csp and 'gstatic' not in csp


@pytest.mark.parametrize('name', sorted(
    p.name for p in FONTS.glob('*.woff2')) or ['missing'])
def test_font_files_are_woff2_and_referenced(client, name):
    path = FONTS / name
    assert path.read_bytes()[:4] == b'wOF2'
    css = (FONTS / 'fonts.css').read_text(encoding='utf-8')
    assert f"url('{name}')" in css
    resp = client.get(f'/static/fonts/{name}')
    assert resp.status_code == 200
    resp.close()


def test_every_font_css_url_exists():
    css = (FONTS / 'fonts.css').read_text(encoding='utf-8')
    urls = re.findall(r"url\('([^']+)'\)", css)
    assert len(urls) == 6
    for url in urls:
        assert (FONTS / url).is_file(), url


def test_each_family_ships_its_ofl_notice():
    for family in ('Inter', 'Syne', 'JetBrainsMono'):
        text = (FONTS / f'OFL-{family}.txt').read_text(encoding='utf-8')
        assert 'SIL OPEN FONT LICENSE Version 1.1' in text
        assert 'Copyright' in text


def test_issue_dialog_says_where_the_report_goes(client):
    csrf_token(client)
    html = client.get('/').get_data(as_text=True)
    start = html.index('id="issueModal"')
    dialog = html[start:html.index('</div>\n    </div>', start)]
    assert 'runs this STATlee server' in dialog
    assert 'can show values from it' in dialog
