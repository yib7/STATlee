"""docs/CREDITS.md names every direct dependency at the version actually shipped:
each pin in requirements/base.txt / requirements/postgres.txt has a row with the
same version, and each vendored front-end file's own header carries the
version CREDITS states."""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CREDITS = (ROOT / 'docs' / 'CREDITS.md').read_text(encoding='utf-8')
VENDOR = ROOT / 'statlee' / 'static' / 'vendor'

# Package name on PyPI -> link text used in the CREDITS table.
LINK_TEXT = {'redis': 'redis-py', 'psycopg': 'psycopg 3'}


def _pins(name):
    pins = {}
    for line in (ROOT / name).read_text(encoding='utf-8').splitlines():
        m = re.match(r'([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==(\S+)', line.split('#')[0])
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


PINS = {**_pins('requirements/base.txt'), **_pins('requirements/postgres.txt')}


@pytest.mark.parametrize('package, version', sorted(PINS.items()))
def test_every_direct_dependency_is_credited_at_its_pin(package, version):
    text = LINK_TEXT.get(package, package)
    row = re.search(r'^\| \[' + re.escape(text) + r'\]\([^)]*\) \| ([^|]+) \|',
                    CREDITS, re.IGNORECASE | re.MULTILINE)
    assert row, f'{package} has no row in docs/CREDITS.md'
    assert row.group(1).strip() == version, (
        f'CREDITS lists {package} {row.group(1).strip()}, pinned {version}')


@pytest.mark.parametrize('filename, pattern, credit', [
    ('purify.min.js', r'DOMPurify (\d+\.\d+\.\d+)', r'\[DOMPurify\]\([^)]*\) (\S+)'),
    ('marked.min.js', r'marked v(\d+\.\d+\.\d+)', r'\[marked\]\([^)]*\) (\S+)'),
    ('codemirror.min.js', r'codemirror@(\d+\.\d+\.\d+)',
     r'\[CodeMirror 5\]\([^)]*\) (\S+)'),
])
def test_vendored_versions_match_credits(filename, pattern, credit):
    head = (VENDOR / filename).read_text(encoding='utf-8')[:400]
    shipped = re.search(pattern, head)
    stated = re.search(credit, CREDITS)
    assert shipped and stated
    assert shipped.group(1) == stated.group(1)
