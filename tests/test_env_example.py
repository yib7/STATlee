""".env.example documents exactly the environment the app reads (D5 docs drift).

The deep audit found MAX_UPLOAD_CELLS, MAX_UPLOAD_UNCOMPRESSED_MB and
RATE_LIMIT_DATA_PAGE read by config.py but missing from .env.example, and
S3_BUCKET/S3_PREFIX documented for a backend that does not exist.
"""
import glob
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Read by code but not operator settings: PATH is forwarded to the sandbox
# child; STATLEE_INPROCESS_MIGRATION is a flag app.py sets for statlee/migrations/env.py.
_INTERNAL = {'PATH', 'STATLEE_INPROCESS_MIGRATION'}
# Documented, consumed outside the Python package (the app image's CMD).
_CONSUMED_ELSEWHERE = {'WEB_CONCURRENCY': 'docker/app.Dockerfile'}

_READ_PATTERNS = (
    re.compile(r"""os\.environ\.get\(\s*['"]([A-Z][A-Z0-9_]*)['"]"""),
    re.compile(r"""os\.environ\[\s*['"]([A-Z][A-Z0-9_]*)['"]\s*\](?!\s*=)"""),
    re.compile(r"""_env_(?:int|bool)\(\s*['"]([A-Z][A-Z0-9_]*)['"]"""),
)


def _vars_read():
    names = set()
    files = glob.glob(os.path.join(REPO, 'statlee', '**', '*.py'), recursive=True)
    files.append(os.path.join(REPO, 'wsgi.py'))
    for path in files:
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        for pattern in _READ_PATTERNS:
            names.update(pattern.findall(src))
    return names - _INTERNAL


def _vars_documented():
    with open(os.path.join(REPO, '.env.example'), encoding='utf-8') as fh:
        return set(re.findall(r'^#?\s*([A-Z][A-Z0-9_]*)=', fh.read(), re.MULTILINE))


def test_every_env_var_the_app_reads_is_documented():
    missing = _vars_read() - _vars_documented()
    assert not missing, f'add to .env.example: {sorted(missing)}'


def test_every_documented_env_var_is_read_somewhere():
    unused = _vars_documented() - _vars_read()
    for name, consumer in _CONSUMED_ELSEWHERE.items():
        with open(os.path.join(REPO, consumer), encoding='utf-8') as fh:
            assert name in fh.read()
        unused.discard(name)
    assert not unused, f'documented but never read: {sorted(unused)}'


def test_gunicorn_worker_count_is_a_number():
    # docker compose passes .env verbatim (env_file), and gunicorn's own config
    # runs int(os.environ['WEB_CONCURRENCY']) at import: an empty value crashed
    # the README Quickstart container before the app loaded.
    with open(os.path.join(REPO, '.env.example'), encoding='utf-8') as fh:
        values = re.findall(r'^WEB_CONCURRENCY=(.*)$', fh.read(), re.MULTILINE)
    for value in values:
        assert value.strip().isdigit(), f'WEB_CONCURRENCY={value!r}'
