"""The compiled lockfiles in requirements/ stay in step with the hand-edited
pins they are built from, and every install path uses them.

requirements/*.txt list the direct dependencies; requirements/*.lock pin and
hash every transitive one (`uv pip compile --universal --generate-hashes`; the
commands are at the top of requirements/base.txt). A pin edited without
re-locking would leave the images and CI installing the old version, so these
checks fail until the locks are regenerated.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQ = ROOT / 'requirements'

# Each lock and the .txt files it is compiled from (dev.txt pulls base.txt in
# with `-r`).
LOCKS = {
    'app.lock': ['base.txt', 'postgres.txt'],
    'dev.lock': ['base.txt', 'dev.txt', 'postgres.txt'],
    'runner.lock': ['runner.txt'],
}


def _norm(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _direct_pins(name):
    pins = {}
    for line in (REQ / name).read_text(encoding='utf-8').splitlines():
        m = re.match(r'([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s;#]+)', line.split('#')[0])
        if m:
            pins[_norm(m.group(1))] = m.group(2)
    return pins


def _locked(name):
    """{package: {versions}} (a package can fork by Python version) and the
    list of requirement entries, each with its hash lines."""
    versions, entries = {}, []
    for block in re.split(r'\n(?=[A-Za-z0-9])', (REQ / name).read_text(encoding='utf-8')):
        m = re.match(r'([A-Za-z0-9_.-]+)==([^\s;\\]+)', block)
        if not m:
            continue
        versions.setdefault(_norm(m.group(1)), set()).add(m.group(2))
        entries.append((m.group(1), block))
    return versions, entries


@pytest.mark.parametrize('lock, sources', sorted(LOCKS.items()))
def test_every_direct_pin_is_locked_at_its_version(lock, sources):
    locked, _ = _locked(lock)
    for source in sources:
        for package, version in _direct_pins(source).items():
            assert locked.get(package) == {version}, (
                f'{source} pins {package}=={version}, {lock} has '
                f'{locked.get(package)}: regenerate the locks (see requirements/base.txt)')


@pytest.mark.parametrize('lock', sorted(LOCKS))
def test_every_locked_package_carries_a_hash(lock):
    _, entries = _locked(lock)
    assert len(entries) > 10, f'{lock} looks empty'
    for package, block in entries:
        assert '--hash=sha256:' in block, f'{lock}: {package} has no hash'


@pytest.mark.parametrize('other', ['dev.lock', 'runner.lock'])
def test_shared_packages_resolve_to_the_app_versions(other):
    """dev.lock and runner.lock are compiled with app.lock as a constraint, so
    the tests and the docker-mode sandbox run the app's exact versions."""
    app, _ = _locked('app.lock')
    theirs, _ = _locked(other)
    for package, versions in theirs.items():
        if package in app:
            assert versions == app[package], (package, other, versions, app[package])


def test_every_install_path_uses_a_lock():
    ci = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
    installs = re.findall(r'pip install[^\n]*', ci)
    for line in installs:
        if '-r ' in line:
            assert re.search(r'-r requirements/(app|dev)\.lock', line), line
            assert '.txt' not in line, line
    audit_job = ci.split('\n  audit:', 1)[1].split('\n  test:', 1)[0]
    assert 'pip-audit' in audit_job
    for lock in LOCKS:
        assert f"'requirements/{lock}'" in audit_job, (
            f'the CI audit job does not read requirements/{lock}')
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    for line in re.findall(r'pip install[^\n]*', readme):
        assert re.search(r'-r requirements/(app|dev)\.lock', line), line
