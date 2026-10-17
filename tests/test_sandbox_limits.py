"""Sandbox resource limits (release audit 2026-10-16, B-01/B-02/B-03/B-10).

- B-01: numeric-library thread fan-out is pinned to 1 and RLIMIT_NPROC is
  relative to the uid's current task count, so a busy host (a desktop with a
  browser, the e2e job's Chromium) no longer kills ``import numpy``, while a
  fork bomb is still bounded.
- B-02: captured output is bounded while it is read, not after.
- B-03: plots and collected files are read back only when they are regular
  files (a symlink or FIFO the script planted is skipped, without blocking).
- B-10: a per-file size limit applies in both modes, total bytes written are
  bounded, and hitting either says so in plain words.
"""
import io
import os
import subprocess
import sys
import threading
import tracemalloc

import pytest

from statlee import sandbox

LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith('linux'),
    reason='RLIMIT_NPROC counting via /proc is Linux-only')
POSIX_ONLY = pytest.mark.skipif(
    os.name != 'posix', reason='rlimits, symlinks and FIFOs are POSIX-only here')


def _is_root():
    return hasattr(os, 'geteuid') and os.geteuid() == 0


# ---------------------------------------------------------------------------
# B-01: thread caps and a relative process cap
# ---------------------------------------------------------------------------

def test_safe_env_pins_every_numeric_thread_pool_to_one(tmp_path):
    env = sandbox._safe_env(str(tmp_path))
    for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'OMP_THREAD_LIMIT',
                'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
                'VECLIB_MAXIMUM_THREADS'):
        assert env[key] == '1', key


def test_docker_runs_get_the_same_thread_caps_and_a_writable_mpl_dir():
    cmd = sandbox._docker_cmd('img', '/tmp/run', 'script.py', True, 2048, 'n',
                              file_limit_mb=5)
    flags = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == '-e']
    assert 'OPENBLAS_NUM_THREADS=1' in flags
    assert 'OMP_NUM_THREADS=1' in flags
    assert 'MPLBACKEND=Agg' in flags
    assert 'MPLCONFIGDIR=/tmp/mpl' in flags
    # B-10: the per-file limit applies in docker mode too.
    assert cmd[cmd.index('--ulimit') + 1] == f'fsize={5 << 20}:{5 << 20}'
    assert cmd[cmd.index('--pids-limit') + 1] == '128'


@POSIX_ONLY
def test_nproc_cap_is_relative_to_the_current_task_count(monkeypatch):
    import resource
    _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)

    def bounded(value):
        return value if hard == resource.RLIM_INFINITY else min(value, hard)

    monkeypatch.setattr(sandbox, '_user_task_count', lambda: 500)
    assert sandbox._nproc_cap() == bounded(500 + sandbox.NPROC_HEADROOM)
    monkeypatch.setattr(sandbox, '_user_task_count', lambda: None)
    assert sandbox._nproc_cap() == bounded(sandbox.NPROC_FALLBACK)


@LINUX_ONLY
def test_user_task_count_sees_this_process_threads():
    stop = threading.Event()
    threads = [threading.Thread(target=stop.wait, daemon=True) for _ in range(20)]
    for t in threads:
        t.start()
    try:
        assert sandbox._user_task_count() >= 21
    finally:
        stop.set()
        for t in threads:
            t.join(5)


@LINUX_ONLY
def test_numpy_import_and_a_new_thread_work_when_the_user_has_many_tasks():
    """The CI failure (B-01): with >=128 tasks already owned by the uid, the
    old absolute RLIMIT_NPROC=128 made OpenBLAS's worker-thread creation fail
    and ``import numpy`` die with KeyboardInterrupt. Hold 140 threads here and
    prove numpy imports and the script can still start one thread."""
    stop = threading.Event()
    held = [threading.Thread(target=stop.wait, daemon=True) for _ in range(140)]
    for t in held:
        t.start()
    try:
        code = ("import threading\n"
                "import numpy as np\n"
                "t = threading.Thread(target=lambda: None)\n"
                "t.start(); t.join()\n"
                "print('NUMPY_OK', float(np.ones(3).sum()))\n")
        res = sandbox.run_in_sandbox(code, 'Python', timeout=60)
    finally:
        stop.set()
        for t in held:          # gone before the next test counts tasks
            t.join(5)
    assert res.success, res.output
    assert 'NUMPY_OK 3.0' in res.output


@LINUX_ONLY
@pytest.mark.skipif(_is_root(), reason='RLIMIT_NPROC is not enforced for root')
def test_nproc_cap_still_bounds_a_fork_loop():
    """Fork-bomb protection stays: a script that forks in a loop is stopped
    by EAGAIN well before it reaches 200 children."""
    code = (
        "import os, time\n"
        "pids, err = [], None\n"
        "for _ in range(200):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError as e:\n"
        "        err = e.errno\n"
        "        break\n"
        "    if pid == 0:\n"
        "        time.sleep(5)\n"
        "        os._exit(0)\n"
        "    pids.append(pid)\n"
        "print('FORKED', len(pids), 'ERRNO', err, flush=True)\n"
        "for p in pids:\n"
        "    os.kill(p, 9)\n"
        "for p in pids:\n"
        "    os.waitpid(p, 0)\n")
    res = sandbox.run_in_sandbox(code, 'Python', timeout=60)
    line = next(ln for ln in res.output.splitlines() if ln.startswith('FORKED'))
    forked, errno_value = int(line.split()[1]), line.split()[3]
    # The cap is the uid's task count plus NPROC_HEADROOM (64), so the loop
    # stops far below 200; the margin absorbs tasks that exit mid-test.
    assert 0 < forked < 200
    assert errno_value == '11'      # EAGAIN


# ---------------------------------------------------------------------------
# B-02: bounded capture
# ---------------------------------------------------------------------------

def test_bounded_reader_keeps_only_the_limit():
    reader = sandbox._BoundedReader(io.BytesIO(b'x' * (10 * 1024 * 1024)), 1000)
    reader.run()
    assert len(reader.kept) == 1000
    assert reader.truncated is True


def test_output_flood_is_bounded_while_it_is_read():
    """~50 MB of stdout must neither reach the app's memory nor the reply."""
    code = ("import sys\n"
            "chunk = 'x' * 1_000_000\n"
            "for _ in range(50):\n"
            "    sys.stdout.write(chunk)\n"
            "print('END')\n")
    limit = 256 * 1024
    tracemalloc.start()
    try:
        res = sandbox.run_in_sandbox(code, 'Python', output_limit=limit,
                                     timeout=60)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert res.success, res.output[-500:]
    assert len(res.output) <= limit + 200
    assert 'output truncated' in res.output
    assert peak < 16 * 1024 * 1024, f'parent held {peak} bytes'


# ---------------------------------------------------------------------------
# B-03: only regular files are read back
# ---------------------------------------------------------------------------

@POSIX_ONLY
def test_symlinked_plot_and_collected_file_are_skipped(tmp_path):
    secret = tmp_path / 'secret.txt'
    secret.write_bytes(b'host secret')
    code = (f"import os\n"
            f"target = ''.join([{str(secret)!r}])\n"
            f"os.symlink(target, 'plot.png')\n"
            f"os.symlink(target, 'out.txt')\n"
            f"print('linked')\n")
    res = sandbox.run_in_sandbox(code, 'Python', collect=('out.txt',))
    assert 'linked' in res.output
    assert res.plots == []
    assert 'out.txt' not in res.files


@POSIX_ONLY
def test_fifo_plot_does_not_block_collection():
    code = "import os\nos.mkfifo('plot.png')\nprint('made fifo')\n"
    box = {}
    worker = threading.Thread(
        target=lambda: box.update(res=sandbox.run_in_sandbox(code, 'Python')),
        daemon=True)
    worker.start()
    worker.join(30)
    assert not worker.is_alive(), 'collecting a FIFO plot blocked the run'
    assert box['res'].plots == []
    assert 'made fifo' in box['res'].output


def test_regular_plot_is_still_collected():
    res = sandbox.run_in_sandbox(
        "open('plot.png', 'wb').write(b'\\x89PNG-ok')", 'Python')
    assert len(res.plots) == 1


# ---------------------------------------------------------------------------
# B-10: file-size limits in plain words
# ---------------------------------------------------------------------------

@POSIX_ONLY
def test_per_file_limit_is_explained():
    code = ("with open('big.bin', 'wb') as fh:\n"
            "    fh.write(b'x' * (3 * 1024 * 1024))\n")
    res = sandbox.run_in_sandbox(code, 'Python', file_limit_mb=1)
    assert not res.success
    assert '1 MB per-file limit' in res.output


def test_total_bytes_written_are_bounded():
    """Many files under the per-file limit still stop at the run-dir cap
    (2 x the per-file limit) instead of filling the disk."""
    code = ("import time\n"
            "for i in range(400):\n"
            "    open(f'part_{i}.bin', 'wb').write(b'x' * (256 * 1024))\n"
            "    time.sleep(0.02)\n"
            "print('wrote everything')\n")
    res = sandbox.run_in_sandbox(code, 'Python', file_limit_mb=1, timeout=60)
    assert not res.success
    assert 'wrote everything' not in res.output
    assert 'wrote more than 2 MB of files' in res.output


def test_file_limit_note_for_sigxfsz():
    if not hasattr(sandbox.signal, 'SIGXFSZ'):
        pytest.skip('no SIGXFSZ on this platform')
    outcome = sandbox._Outcome(returncode=-sandbox.signal.SIGXFSZ)
    assert '256 MB per-file limit' in sandbox._file_limit_note(outcome, 256, 512)


def test_missing_interpreter_is_reported(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError('Rscript')
    monkeypatch.setattr(subprocess, 'Popen', missing)
    res = sandbox.run_in_sandbox('print(1)', 'R')
    assert res.returncode == -1
    assert 'Interpreter not available' in res.output
