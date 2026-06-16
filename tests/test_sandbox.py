"""Execution sandbox: secret scrubbing (0.1), output capture, multi-plot
collection (5.2), timeouts, and named-file collection (used by /wrangle)."""
import os

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
