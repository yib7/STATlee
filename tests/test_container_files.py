"""Container build/runtime files (D5-4, D5-5, D5-9, D5-12, D5-17).

These are text checks on files the test suite cannot execute (no image is
built here); they pin the properties the deep audit found missing.
"""
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _read(name):
    with open(os.path.join(REPO, name), encoding='utf-8') as fh:
        return fh.read()


def _lines(name):
    return [ln.strip() for ln in _read(name).splitlines()
            if ln.strip() and not ln.strip().startswith('#')]


def _pins(name):
    pins = {}
    for line in _lines(name):
        line = line.split('#', 1)[0].strip()
        m = re.match(r'^([A-Za-z0-9_.\-\[\]]+)==([^\s;]+)', line)
        if m:
            pins[m.group(1).lower().split('[')[0]] = m.group(2)
    return pins


def test_dockerignore_keeps_secrets_local_data_and_vcs_out_of_the_image():
    """D5-5: without a .dockerignore, `COPY . .` baked the untracked .env (real
    provider keys), instance/*.db (accounts, password hashes) and .git into the
    image, and load_dotenv() then loaded the builder's key at runtime."""
    patterns = set(_lines('.dockerignore'))
    for required in ('.env', '.env.*', '!.env.example', 'instance/', '*.db',
                     '.git', '.autopilot/', '.claude/', 'CLAUDE.md',
                     '__pycache__/', '.pytest_cache/', 'venv/', '.venv/',
                     '_repo-backup/', '_logo-work/', 'tests/'):
        assert required in patterns, required


def test_compose_forwards_the_documented_env_file():
    """D5-4: compose forwarded only 12 variables, silently dropping the
    money-safety, rate-limit, model, SMTP and quota settings in .env."""
    compose = _read('docker-compose.yml')
    assert re.search(r'env_file:\s*\n\s*-\s*path:\s*\.env\s*\n\s*required:\s*false',
                     compose)
    # The masked host .env stays out of the container filesystem.
    assert '/dev/null:/app/.env:ro' in compose


def test_dockerfile_runs_as_a_non_root_user():
    """D5-17: generated code in subprocess mode ran as root in the container."""
    lines = _lines('Dockerfile')
    users = [ln.split(None, 1)[1] for ln in lines if ln.upper().startswith('USER ')]
    assert users, 'Dockerfile has no USER instruction'
    assert users[-1].split(':')[0] not in ('root', '0')
    last_copy = max(i for i, ln in enumerate(lines) if ln.upper().startswith('COPY '))
    last_user = max(i for i, ln in enumerate(lines) if ln.upper().startswith('USER '))
    assert last_user > last_copy


def test_dockerfile_pins_a_persistent_upload_root():
    """D5-9: the image set no UPLOAD_ROOT, so storage moved on every restart."""
    assert 'UPLOAD_ROOT=/app/instance/uploads' in _read('Dockerfile')


def test_app_image_installs_the_postgres_driver():
    """D5-11: the documented production DATABASE_URL (postgresql+psycopg://)
    failed at boot with ModuleNotFoundError: psycopg."""
    assert 'psycopg' in _read('requirements-postgres.txt')
    assert re.search(r'pip install[^\n]*-r requirements-postgres\.txt',
                     _read('Dockerfile'))


def test_runner_image_pins_match_the_app():
    """D5-12: docker mode and subprocess mode must run the same analysis
    library versions (the runner pinned matplotlib 3.10.8 vs the app's 3.11.0)."""
    app_pins = _pins('requirements.txt')
    runner_pins = _pins('requirements-runner.txt')
    assert runner_pins, 'requirements-runner.txt has no pins'
    for pkg, version in runner_pins.items():
        assert app_pins.get(pkg) == version, (pkg, version, app_pins.get(pkg))
    assert 'requirements-runner.txt' in _read('runner.Dockerfile')


def test_ruff_version_is_the_same_in_ci_pre_commit_and_requirements_dev():
    """The pre-commit hook pinned ruff v0.15.17 after requirements-dev.txt and
    the CI lint job moved to 0.17.1, so a contributor's hook linted with a
    different rule set than the gate that blocks the merge."""
    dev = _pins('requirements-dev.txt')['ruff']
    ci = re.search(r'pip install ruff==([^\s]+)', _read('.github/workflows/ci.yml')).group(1)
    hook = re.search(r'ruff-pre-commit\s*\n\s*rev:\s*v?([^\s]+)',
                     _read('.pre-commit-config.yaml')).group(1)
    assert ci == dev, f'ci.yml ruff {ci} != requirements-dev.txt {dev}'
    assert hook == dev, f'.pre-commit-config.yaml ruff {hook} != requirements-dev.txt {dev}'
