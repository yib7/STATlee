"""Isolated execution of untrusted analysis code (roadmap Tier 0).

- 0.1 The subprocess gets an explicit, secret-free environment — generated
      code cannot read GEMINI_API_KEY or any other app secret.
- 0.2 Every run gets its own throwaway working directory containing ONLY the
      one dataset it needs; concurrent runs can never see each other's files.
      On POSIX, resource limits cap memory/CPU/file-size/processes (no-op on
      the Windows dev host). Captured output is bounded WHILE it is read, so a
      script that floods stdout cannot exhaust the app's memory.
- 0.3 Optional true container isolation: SANDBOX_MODE=docker launches a
      sibling container (network-less, non-root, read-only, resource-capped)
      per execution using the image built from docker/runner.Dockerfile.
- 5.2 All ``plot*.png`` files produced by the run are collected, not just one,
      up to ``PLOTS_MAX`` plots and ``PLOTS_BYTES_MAX`` bytes of them per run
      (every run's plots are kept in the session workbook). Only regular files
      are read back: a symlink or FIFO the script planted is skipped.
"""
import base64
import glob
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger('statlee.sandbox')

# Plots kept per run (in name order). Each run's plots are stored in the
# session workbook (statlee/runs.py), which counts toward the identity's
# MAX_BYTES_PER_IDENTITY, so a script writing hundreds of images is cut off
# here; the output says how many were not kept.
PLOTS_MAX = 20
PLOTS_BYTES_MAX = 10 * 1024 * 1024     # 10 MB of PNG bytes per run

# Largest single file a run may write (config EXEC_FILE_LIMIT_MB). It sits
# above the biggest CSV MAX_UPLOAD_CELLS allows, so a /wrangle that rewrites
# the dataset is never cut off by it.
FILE_LIMIT_MB_DEFAULT = 256
# All files a run writes together may not exceed this many times the per-file
# limit (on top of the dataset copied in). Checked while the run is going, so
# a script writing many files cannot fill the disk the DB and uploads share.
RUN_DIR_FILES_FACTOR = 2
_DIR_POLL_SECONDS = 0.25

# Thread fan-out of the numeric libraries (B-01). One thread each: OpenBLAS,
# MKL, OpenMP (R's data.table too) and numexpr then start no worker threads at
# all. RLIMIT_CPU counts CPU across threads, so extra BLAS threads only spent
# the CPU budget faster, and each one counts against RLIMIT_NPROC.
THREAD_ENV = {
    'OPENBLAS_NUM_THREADS': '1',
    'OMP_NUM_THREADS': '1',
    'OMP_THREAD_LIMIT': '1',
    'MKL_NUM_THREADS': '1',
    'NUMEXPR_NUM_THREADS': '1',
    'VECLIB_MAXIMUM_THREADS': '1',
}

# RLIMIT_NPROC counts every task (process or thread) of the real uid across
# the whole machine, so an absolute cap fails as soon as the app user already
# runs that many (a desktop session with a browser open, the e2e job's
# Chromium). The cap is set relative to the uid's current task count: the run
# may add at most NPROC_HEADROOM tasks, which is the fork-bomb bound that
# matters. NPROC_FALLBACK is used when the count cannot be read.
NPROC_HEADROOM = 64
NPROC_FALLBACK = 128

_root_warned = False

# Run output (tracebacks above all) is shown in the UI, stored with the run and
# shipped in /export, so host paths in it are rewritten to neutral labels
# (Phase 4, 4.9): the run dir and the docker workdir disappear (``File
# "script.py"``), the interpreter's install dirs become ``<site-packages>``,
# ``<stdlib>`` or ``<python>``, the temp dir ``<tmp>`` and the home dir ``~``.
DOCKER_WORKDIR = '/work'
# A replaced path must end at a separator, quote, space or punctuation, so
# "/app" never rewrites "/apple".
_PATH_END = r"(?=[\\/\s\"':,;)\]>]|$)"
_HOST_PREFIXES = None


def _path_pattern(path):
    """Regex for ``path`` that matches either separator, so ``C:\\x\\y`` and
    ``C:/x/y`` are both found."""
    parts = [re.escape(part) for part in re.split(r'[\\/]+', path) if part]
    lead = r'[\\/]+' if path[:1] in ('/', '\\') else ''
    return lead + r'[\\/]+'.join(parts)


def _host_prefixes():
    """(path, label) pairs for the host's install, temp and home dirs,
    longest first."""
    import site
    import sysconfig
    paths = sysconfig.get_paths()
    site_dirs = [paths.get('purelib'), paths.get('platlib')]
    try:
        site_dirs += list(site.getsitepackages())
        site_dirs.append(site.getusersitepackages())
    except AttributeError:   # some embedded/venv layouts lack these helpers
        pass
    pairs = [(d, '<site-packages>') for d in site_dirs]
    pairs += [(paths.get('stdlib'), '<stdlib>'),
              (paths.get('platstdlib'), '<stdlib>')]
    pairs += [(d, '<python>') for d in (sys.prefix, sys.base_prefix,
                                        sys.exec_prefix,
                                        os.path.dirname(sys.executable))]
    pairs += [(tempfile.gettempdir(), '<tmp>'), (os.path.expanduser('~'), '~')]
    seen, out = set(), []
    for path, label in pairs:
        if not path:
            continue
        for variant in {os.path.normpath(path), os.path.realpath(path)}:
            # A drive root or "/" would rewrite every absolute path.
            if len(variant.strip('\\/')) <= 2 or variant in seen:
                continue
            seen.add(variant)
            out.append((variant, label))
    return sorted(out, key=lambda pair: len(pair[0]), reverse=True)


def redact_host_paths(text, run_dir=None):
    """``text`` with host paths replaced by neutral labels (see above)."""
    global _HOST_PREFIXES
    if not text:
        return text
    if _HOST_PREFIXES is None:
        _HOST_PREFIXES = _host_prefixes()
    flags = re.IGNORECASE if os.name == 'nt' else 0
    run_dirs = {DOCKER_WORKDIR}
    if run_dir:
        run_dirs.update({run_dir, os.path.realpath(run_dir)})
    for path in sorted(run_dirs, key=len, reverse=True):
        pattern = _path_pattern(path)
        text = re.sub(pattern + r'[\\/]+', '', text, flags=flags)
        text = re.sub(pattern + _PATH_END, '.', text, flags=flags)
    for path, label in _HOST_PREFIXES:
        text = re.sub(_path_pattern(path) + _PATH_END, label, text,
                      flags=flags)
    return text


def plot_limit_note(skipped):
    return (f'\n[{skipped} more plot(s) not kept: a run keeps at most '
            f'{PLOTS_MAX} plots and {PLOTS_BYTES_MAX // (1024 * 1024)} MB '
            'of plots.]\n')


@dataclass
class RunResult:
    output: str = ''
    plots: list = field(default_factory=list)   # base64-encoded PNGs
    files: dict = field(default_factory=dict)   # collected output files (bytes)
    returncode: int = 0
    timed_out: bool = False

    @property
    def success(self):
        return self.returncode == 0 and not self.timed_out


def _safe_env(run_dir):
    """Minimal, secret-free environment (0.1)."""
    env = {
        'PATH': os.environ.get('PATH', ''),
        'HOME': run_dir,
        'MPLBACKEND': 'Agg',           # force headless matplotlib
        'LANG': 'C.UTF-8',
        'PYTHONIOENCODING': 'utf-8',
        **THREAD_ENV,
    }
    if os.name == 'nt':
        # Windows dev host: CPython and matplotlib need these to function.
        # APPDATA/LOCALAPPDATA are required for Python to locate user-site
        # packages (where pip installs by default on Windows). They are plain
        # paths, not secrets. Production runs on Linux/Docker where analysis
        # libraries live in system site-packages and none of this applies.
        for key in ('SYSTEMROOT', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT',
                    'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
                    'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'):
            if key in os.environ:
                env[key] = os.environ[key]
        env['TEMP'] = env['TMP'] = run_dir
    return env


def _user_task_count():
    """Tasks (processes plus threads) the current uid owns, read from
    ``/proc``. None where that is unavailable (not Linux, ``/proc`` hidden)."""
    if not sys.platform.startswith('linux'):
        return None
    uid = os.getuid()
    try:
        entries = os.listdir('/proc')
    except OSError:
        return None
    total = 0
    for name in entries:
        if not name.isdigit():
            continue
        try:
            if os.stat(f'/proc/{name}').st_uid != uid:
                continue
            total += len(os.listdir(f'/proc/{name}/task'))
        except OSError:
            continue   # the process exited during the scan
    return total or None


def _nproc_cap():
    """The RLIMIT_NPROC value for the next run: the uid's current task count
    plus ``NPROC_HEADROOM``, never above the existing hard limit (raising it
    needs privileges, and a failed setrlimit would leave no cap at all)."""
    import resource  # noqa: PLC0415 — POSIX-only import
    current = _user_task_count()
    cap = NPROC_FALLBACK if current is None else current + NPROC_HEADROOM
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    except (ValueError, OSError):
        return cap
    if hard != resource.RLIM_INFINITY:
        cap = min(cap, hard)
    return cap


def _warn_if_root():
    global _root_warned
    if _root_warned or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        return
    _root_warned = True
    logger.warning(
        "The app runs as root, so RLIMIT_NPROC is not enforced on sandboxed "
        "scripts (a fork bomb is not bounded). Run it as an unprivileged user "
        "or use SANDBOX_MODE=docker.")


def _posix_limits(memory_mb, file_limit_mb=FILE_LIMIT_MB_DEFAULT):
    """Build a preexec_fn applying rlimits. POSIX only (0.2).

    The process cap is computed here, in the parent, because the closure runs
    between fork and exec where reading ``/proc`` is unsafe."""
    if os.name == 'nt':
        return None
    import resource  # noqa: PLC0415 — POSIX-only import

    _warn_if_root()
    nproc = _nproc_cap()
    file_bytes = int(file_limit_mb) * 1024 * 1024

    def set_limits():
        mem_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
        except (ValueError, OSError):
            pass  # not adjustable in some containers
    set_limits.nproc = nproc
    return set_limits


# ---------------------------------------------------------------------------
# Output capture (B-02): bounded while it is read
# ---------------------------------------------------------------------------

class _BoundedReader(threading.Thread):
    """Drain a pipe, keeping only its first ``limit`` bytes. The rest is read
    and dropped, so the child never blocks on a full pipe and the app never
    holds more than ``limit`` bytes of one stream."""

    CHUNK = 64 * 1024

    def __init__(self, stream, limit):
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = max(0, int(limit))
        self.kept = bytearray()
        self.truncated = False

    def run(self):
        try:
            while True:
                chunk = self.stream.read(self.CHUNK)
                if not chunk:
                    break
                room = self.limit - len(self.kept)
                if room > 0:
                    self.kept += chunk[:room]
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except (OSError, ValueError):
            pass   # pipe closed under us (the process was killed)
        finally:
            try:
                self.stream.close()
            except OSError:
                pass

    def text(self):
        return bytes(self.kept).decode('utf-8', 'replace')


def _truncate(text, limit, force=False):
    if text and (len(text) > limit or force):
        return text[:limit] + f"\n... [output truncated at {limit // 1024} KB]"
    return text or ''


def _combine_output(stdout, stderr, output_limit, truncated=False):
    """Merge stdout/stderr into the single captured-output string the app shows."""
    output = stdout or ''
    if stderr:
        output += f"\n--- Output/Warnings ---\n{stderr}"
    return _truncate(output.strip(), output_limit, force=truncated)


def _timeout_message(timeout):
    return (f"Execution timed out after {timeout} seconds. "
            "Code took too long to run.")


def _timeout_output(partial, timeout):
    """What the script printed before it was stopped, then the timeout notice
    (S5): a long model fit that dies at the limit still shows its progress."""
    notice = _timeout_message(timeout)
    return f"{partial}\n\n{notice}" if partial else notice


def _kill_subprocess(proc):
    """Kill ``proc`` on timeout. On POSIX the child leads its own process group
    (start_new_session=True), so signal the whole group to reap orphaned
    grandchildren too; on Windows there is no process-group kill, so fall back
    to killing the direct child only."""
    if os.name == 'posix' and hasattr(os, 'killpg'):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, OSError):
            pass  # already gone, or no group; fall through to a direct kill
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _dir_bytes(path):
    """Bytes of the regular files under ``path`` (symlinks not followed)."""
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += _dir_bytes(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


@dataclass
class _Outcome:
    returncode: int = 0
    stdout: str = ''
    stderr: str = ''
    truncated: bool = False
    timed_out: bool = False
    disk_exceeded: bool = False


def _execute(cmd, popen_kwargs, *, timeout, output_limit, kill, run_dir=None,
             dir_limit=None):
    """Run ``cmd`` to completion, a timeout, or the run-dir byte limit.

    stdout/stderr are drained by ``_BoundedReader`` threads (each keeps at
    most ``output_limit`` bytes); ``kill(proc)`` stops the run. Raises
    FileNotFoundError when the interpreter (or docker) is missing."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **popen_kwargs)
    readers = [_BoundedReader(proc.stdout, output_limit),
               _BoundedReader(proc.stderr, output_limit)]
    for reader in readers:
        reader.start()
    outcome = _Outcome()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            outcome.timed_out = True
            break
        try:
            proc.wait(timeout=min(_DIR_POLL_SECONDS, remaining))
            break
        except subprocess.TimeoutExpired:
            if dir_limit and run_dir and _dir_bytes(run_dir) > dir_limit:
                outcome.disk_exceeded = True
                break
    if outcome.timed_out or outcome.disk_exceeded:
        kill(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.error("Sandboxed process %s did not exit after kill", proc.pid)
    for reader in readers:
        # A grandchild that escaped the kill could hold a pipe open; never
        # pin the request thread on it.
        reader.join(timeout=5)
    outcome.returncode = proc.returncode if proc.returncode is not None else -1
    outcome.stdout, outcome.stderr = readers[0].text(), readers[1].text()
    outcome.truncated = any(r.truncated for r in readers)
    return outcome


def _file_limit_note(outcome, file_limit_mb, dir_limit_mb):
    """A plain explanation when the run hit a file-size limit (B-10): Python
    raises ``OSError: [Errno 27] File too large`` and R is killed by
    SIGXFSZ, neither of which says which limit or how big it is."""
    if outcome.disk_exceeded:
        return (f'\n[Run stopped: the script wrote more than {dir_limit_mb} MB '
                'of files. Write fewer or smaller files.]')
    xfsz = getattr(signal, 'SIGXFSZ', None)
    text = f'{outcome.stdout}\n{outcome.stderr}'
    if ((xfsz is not None and outcome.returncode == -xfsz)
            or 'File too large' in text):
        return (f'\n[A file the script wrote exceeded the {file_limit_mb} MB '
                'per-file limit (EXEC_FILE_LIMIT_MB).]')
    return ''


def _docker_cmd(image, run_dir, script_name, is_python, memory_mb, name,
                file_limit_mb=FILE_LIMIT_MB_DEFAULT):
    """Sibling-container invocation (0.3, option A)."""
    interpreter = 'python' if is_python else 'Rscript'
    env_flags = []
    for key, value in {**THREAD_ENV, 'MPLBACKEND': 'Agg',
                       # The rootfs is read-only: give matplotlib (font cache)
                       # and HOME a writable spot instead of a warning per run.
                       'MPLCONFIGDIR': '/tmp/mpl', 'HOME': '/tmp'}.items():
        env_flags += ['-e', f'{key}={value}']
    file_bytes = int(file_limit_mb) * 1024 * 1024
    return [
        'docker', 'run', '--rm', '--name', name,
        '--network', 'none',
        '--read-only',
        '--user', '1000:1000',
        '--memory', f'{memory_mb}m',
        '--cpus', '1',
        '--pids-limit', '128',
        '--ulimit', f'fsize={file_bytes}:{file_bytes}',
        '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges',
        '--tmpfs', '/tmp:rw,size=64m',
        *env_flags,
        '-v', f'{run_dir}:/work:rw',
        '-w', '/work',
        image, interpreter, script_name,
    ]


def _read_regular_file(path, max_bytes):
    """Up to ``max_bytes`` of ``path``, or None unless it is a regular file
    (B-03). The script chose these names, so a symlink (which in docker mode
    would point the host-side app at a host file) or a FIFO (whose open would
    block forever) is skipped: ``lstat`` first, then an ``O_NOFOLLOW`` /
    ``O_NONBLOCK`` open re-checked with ``fstat``."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
    except OSError:
        return None
    flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        return fh.read(max_bytes)


def run_in_sandbox(code, language='Python', dataset_path=None,
                   dataset_name=None, *, timeout=60, memory_mb=2048,
                   output_limit=256 * 1024, mode='subprocess',
                   runner_image='statlee-runner', work_root=None, collect=(),
                   file_limit_mb=FILE_LIMIT_MB_DEFAULT):
    """Execute ``code`` in a throwaway directory and collect its artifacts.

    ``work_root`` (config ``SANDBOX_WORK_ROOT``), when set, is the parent under
    which each per-run throwaway dir is created instead of the system temp dir.
    In ``SANDBOX_MODE=docker`` with the app itself containerized and the host
    docker socket mounted, ``docker run -v`` is resolved by the HOST daemon
    against the HOST filesystem; a run dir in the app container's private /tmp
    does not exist host-side. Pointing this at a path the operator bind-mounts
    at the SAME absolute path into the app container lets the daemon resolve
    the ``-v`` source (P1-5).

    ``file_limit_mb`` (config ``EXEC_FILE_LIMIT_MB``) caps each file the run
    writes, in both modes; all files together may add at most
    ``RUN_DIR_FILES_FACTOR`` times that to the run dir.
    """
    is_python = (language or 'Python').lower() == 'python'
    file_limit_mb = max(1, int(file_limit_mb or FILE_LIMIT_MB_DEFAULT))
    if work_root:
        os.makedirs(work_root, exist_ok=True)
        run_dir = tempfile.mkdtemp(prefix='ccrun_', dir=work_root)
    else:
        run_dir = tempfile.mkdtemp(prefix='ccrun_')
    # docker mode pins the runner to --user 1000:1000, but mkdtemp creates
    # run_dir mode 0700 owned by the app user. Unless the app uid is exactly
    # 1000 the runner cannot read /work, so widen the throwaway dir to
    # world-accessible on the POSIX docker path. chmod's permission bits are a
    # POSIX concept; docker mode is never exercised on the Windows dev host, so
    # the chmod is guarded out there and behavior stays unchanged (P1-5).
    if mode == 'docker' and os.name == 'posix':
        os.chmod(run_dir, 0o777)
    result = RunResult()
    try:
        if dataset_path and os.path.exists(dataset_path):
            # The generated code references the dataset by its client-facing
            # name; copy the ACTIVE version's bytes in under that name.
            target = os.path.join(run_dir, dataset_name or os.path.basename(dataset_path))
            shutil.copyfile(dataset_path, target)

        script_name = 'script.py' if is_python else 'script.R'
        script_path = os.path.join(run_dir, script_name)
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(code)

        dir_limit_mb = RUN_DIR_FILES_FACTOR * file_limit_mb
        dir_limit = _dir_bytes(run_dir) + dir_limit_mb * 1024 * 1024

        if mode == 'docker':
            container = os.path.basename(run_dir)
            cmd = _docker_cmd(runner_image, run_dir, script_name, is_python,
                              memory_mb, container, file_limit_mb)

            def kill(proc):
                # Kill the container by name before the finally block removes
                # its bind-mounted run_dir, then the docker client itself.
                # The client shares the app's process group, so it is killed
                # directly (never through killpg).
                try:
                    subprocess.run(['docker', 'kill', container],
                                   capture_output=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    logger.warning("docker kill %s failed", container)
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
            popen_kwargs = {}
        else:
            interpreter = sys.executable if is_python else 'Rscript'
            cmd = [interpreter, script_path]
            popen_kwargs = {'cwd': run_dir, 'env': _safe_env(run_dir)}
            preexec = _posix_limits(memory_mb, file_limit_mb)
            if preexec:
                popen_kwargs['preexec_fn'] = preexec
            if os.name == 'posix':
                # New session -> the child leads its own process group, so a
                # timeout can kill the whole tree (grandchildren included), not
                # just the direct child. POSIX-only; unchanged on Windows.
                popen_kwargs['start_new_session'] = True
            kill = _kill_subprocess

        try:
            outcome = _execute(cmd, popen_kwargs, timeout=timeout,
                               output_limit=output_limit, kill=kill,
                               run_dir=run_dir, dir_limit=dir_limit)
        except FileNotFoundError as e:
            result.returncode = -1
            result.output = f"Interpreter not available: {e}"
        else:
            output = _combine_output(outcome.stdout, outcome.stderr,
                                     output_limit, outcome.truncated)
            output += _file_limit_note(outcome, file_limit_mb, dir_limit_mb)
            if outcome.timed_out:
                # The script ran and was stopped; what it wrote before the
                # stop is kept (S5).
                result.timed_out = True
                result.returncode = -1
                result.output = _timeout_output(output, timeout)
            else:
                result.returncode = (-1 if outcome.disk_exceeded
                                     else outcome.returncode)
                result.output = output

        plot_bytes = skipped = 0
        for plot_path in sorted(glob.glob(os.path.join(run_dir, 'plot*.png'))):
            try:
                if len(result.plots) >= PLOTS_MAX:
                    skipped += 1
                    continue
                data = _read_regular_file(plot_path,
                                          PLOTS_BYTES_MAX - plot_bytes + 1)
                if data is None:
                    logger.warning("Skipped a plot that is not a regular file")
                    continue
                if plot_bytes + len(data) > PLOTS_BYTES_MAX:
                    skipped += 1
                    continue
                plot_bytes += len(data)
                result.plots.append(base64.b64encode(data).decode('utf-8'))
            except OSError:
                logger.warning("Could not read plot file %s", plot_path)
        if skipped:
            result.output = (result.output or '') + plot_limit_note(skipped)

        for name in collect:
            path = os.path.join(run_dir, name)
            try:
                data = _read_regular_file(path, file_limit_mb * 1024 * 1024)
            except OSError:
                logger.warning("Could not collect output file %s", name)
                continue
            if data is not None:
                result.files[name] = data
        result.output = redact_host_paths(result.output, run_dir)
        return result
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
