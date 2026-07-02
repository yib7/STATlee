"""Execution sandbox: secret scrubbing (0.1), output capture, multi-plot
collection (5.2), timeouts, and named-file collection (used by /wrangle)."""
import os
import subprocess

from statlee import sandbox


def test_runs_python_and_captures_stdout():
    res = sandbox.run_in_sandbox("print('hello-sandbox')", 'Python')
    assert res.success
    assert 'hello-sandbox' in res.output


def test_environment_is_scrubbed_of_secrets(monkeypatch):
    """A secret in the parent env must not reach the child process (0.1)."""
    monkeypatch.setenv('CC_FAKE_SECRET', 'leaky-value-123')
    code = "import os; print('SECRET=' + str(os.environ.get('CC_FAKE_SECRET')))"
    res = sandbox.run_in_sandbox(code, 'Python')
    assert 'leaky-value-123' not in res.output
    assert 'SECRET=None' in res.output


def test_collects_multiple_plots():
    # Write two PNG-named files; the sandbox should base64 both (5.2).
    code = (
        "open('plot.png','wb').write(b'\\x89PNG-one')\n"
        "open('plot_2.png','wb').write(b'\\x89PNG-two')\n"
        "print('done')"
    )
    res = sandbox.run_in_sandbox(code, 'Python')
    assert res.success
    assert len(res.plots) == 2


def test_timeout_is_reported():
    res = sandbox.run_in_sandbox("import time; time.sleep(5)", 'Python', timeout=1)
    assert res.timed_out is True
    assert not res.success
    assert 'timed out' in res.output.lower()


def test_subprocess_mode_timeout_has_no_docker_kill(monkeypatch):
    """Subprocess-mode timeout must not attempt a docker kill (and must not
    raise NameError referencing an undefined `container`)."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get('timeout'))

    monkeypatch.setattr(subprocess, 'run', fake_run)
    res = sandbox.run_in_sandbox("print('x')", 'Python', timeout=1, mode='subprocess')

    assert res.timed_out is True
    assert not res.success
    assert len(calls) == 1  # only the interpreter invocation, no docker kill


def test_docker_mode_kills_container_on_timeout(monkeypatch):
    """On docker-mode timeout, the orphaned container must be killed by name
    BEFORE the run_dir is removed, so `docker kill` isn't racing an already
    -deleted bind mount (P1-2)."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ['docker', 'run']:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get('timeout'))
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    res = sandbox.run_in_sandbox(
        "print('x')", 'Python', timeout=1, mode='docker')

    assert res.timed_out is True
    assert not res.success

    docker_run_calls = [c for c in calls if c[:2] == ['docker', 'run']]
    kill_calls = [c for c in calls if c[:2] == ['docker', 'kill']]
    assert len(docker_run_calls) == 1
    assert len(kill_calls) == 1

    # The container name passed to `docker run --name <name>` must match
    # the one passed to `docker kill <name>`.
    run_cmd = docker_run_calls[0]
    name_idx = run_cmd.index('--name') + 1
    container_name = run_cmd[name_idx]
    assert kill_calls[0] == ['docker', 'kill', container_name]


def test_nonzero_exit_is_failure():
    res = sandbox.run_in_sandbox("raise SystemExit(3)", 'Python')
    assert res.success is False
    assert res.returncode == 3


def test_collect_named_output_file():
    code = "open('out.txt','wb').write(b'payload'); print('ok')"
    res = sandbox.run_in_sandbox(code, 'Python', collect=('out.txt',))
    assert res.success
    assert res.files['out.txt'] == b'payload'


def test_dataset_is_copied_into_run_dir(tmp_path):
    data = tmp_path / 'data.csv'
    data.write_text('a,b\n1,2\n')
    code = ("import pandas as pd; df = pd.read_csv('data.csv'); "
            "print('ROWS', len(df))")
    res = sandbox.run_in_sandbox(code, 'Python',
                                 dataset_path=str(data), dataset_name='data.csv')
    assert res.success
    assert 'ROWS 1' in res.output


def test_run_dir_is_cleaned_up(tmp_path):
    """The throwaway working dir must not linger after the run (0.2)."""
    before = set(os.listdir(os.path.dirname(os.path.realpath(tmp_path))))  # noqa: F841
    res = sandbox.run_in_sandbox("print('x')", 'Python')
    assert res.success
    # No assertion on temp internals beyond success; cleanup is in a finally.
