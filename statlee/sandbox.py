"""Isolated execution of untrusted analysis code (roadmap Tier 0).

- 0.1 The subprocess gets an explicit, secret-free environment — generated
      code cannot read GEMINI_API_KEY or any other app secret.
- 0.2 Every run gets its own throwaway working directory containing ONLY the
      one dataset it needs; concurrent runs can never see each other's files.
      On POSIX, resource limits cap memory/CPU/file-size/processes (no-op on
      the Windows dev host). Captured output is truncated.
- 0.3 Optional true container isolation: SANDBOX_MODE=docker launches a
      sibling container (network-less, non-root, read-only, resource-capped)
      per execution using the image built from runner.Dockerfile.
- 5.2 All ``plot*.png`` files produced by the run are collected, not just one,
      up to ``PLOTS_MAX`` plots and ``PLOTS_BYTES_MAX`` bytes of them per run
      (every run's plots are kept in the session workbook).
"""
import base64
import glob
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

logger = logging.getLogger('statlee.sandbox')

# Plots kept per run (in name order). Each run's plots are stored in the
# session workbook (statlee/runs.py), which counts toward the identity's
# MAX_BYTES_PER_IDENTITY, so a script writing hundreds of images is cut off
# here; the output says how many were not kept.
PLOTS_MAX = 20
PLOTS_BYTES_MAX = 10 * 1024 * 1024     # 10 MB of PNG bytes per run


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
        'OPENBLAS_NUM_THREADS': '2',   # cap BLAS thread fan-out
        'LANG': 'C.UTF-8',
        'PYTHONIOENCODING': 'utf-8',
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


def _posix_limits(memory_mb):
    """Build a preexec_fn applying rlimits. POSIX only (0.2)."""
    if os.name == 'nt':
        return None
    import resource  # noqa: PLC0415 — POSIX-only import

    def set_limits():
        mem_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
        except (ValueError, OSError):
            pass  # not adjustable in some containers
    return set_limits


def _truncate(text, limit):
    if text and len(text) > limit:
        return text[:limit] + f"\n... [output truncated at {limit // 1024} KB]"
    return text or ''


def _combine_output(stdout, stderr, output_limit):
    """Merge stdout/stderr into the single captured-output string the app shows."""
    output = stdout or ''
    if stderr:
        output += f"\n--- Output/Warnings ---\n{stderr}"
    return _truncate(output.strip(), output_limit)


def _timeout_message(timeout):
    return (f"Execution timed out after {timeout} seconds. "
            "Code took too long to run.")


def _as_text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value or ''


def _timeout_output(stdout, stderr, timeout, output_limit):
    """What the script printed before it was stopped, then the timeout notice
    (S5): a long model fit that dies at the limit still shows its progress."""
    partial = _combine_output(_as_text(stdout), _as_text(stderr), output_limit)
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


def _docker_cmd(image, run_dir, script_name, is_python, memory_mb, name):
    """Sibling-container invocation (0.3, option A)."""
    interpreter = 'python' if is_python else 'Rscript'
    return [
        'docker', 'run', '--rm', '--name', name,
        '--network', 'none',
        '--read-only',
        '--user', '1000:1000',
        '--memory', f'{memory_mb}m',
        '--cpus', '1',
        '--pids-limit', '128',
        '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges',
        '--tmpfs', '/tmp:rw,size=64m',
        '-v', f'{run_dir}:/work:rw',
        '-w', '/work',
        image, interpreter, script_name,
    ]


def run_in_sandbox(code, language='Python', dataset_path=None,
                   dataset_name=None, *, timeout=60, memory_mb=2048,
                   output_limit=256 * 1024, mode='subprocess',
                   runner_image='statlee-runner', work_root=None, collect=()):
    """Execute ``code`` in a throwaway directory and collect its artifacts.

    ``work_root`` (config ``SANDBOX_WORK_ROOT``), when set, is the parent under
    which each per-run throwaway dir is created instead of the system temp dir.
    In ``SANDBOX_MODE=docker`` with the app itself containerized and the host
    docker socket mounted, ``docker run -v`` is resolved by the HOST daemon
    against the HOST filesystem; a run dir in the app container's private /tmp
    does not exist host-side. Pointing this at a path the operator bind-mounts
    at the SAME absolute path into the app container lets the daemon resolve
    the ``-v`` source (P1-5).
    """
    is_python = (language or 'Python').lower() == 'python'
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

        if mode == 'docker':
            container = os.path.basename(run_dir)
            cmd = _docker_cmd(runner_image, run_dir, script_name, is_python,
                              memory_mb, container)
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout)
                result.returncode = proc.returncode
                result.output = _combine_output(
                    proc.stdout, proc.stderr, output_limit)
            except subprocess.TimeoutExpired as exc:
                # Kill the orphaned container by name before the finally block
                # removes its bind-mounted run_dir.
                subprocess.run(['docker', 'kill', container],
                               capture_output=True, timeout=10)
                result.timed_out = True
                result.returncode = -1
                result.output = _timeout_output(
                    exc.stdout, exc.stderr, timeout, output_limit)
            except FileNotFoundError as e:
                result.returncode = -1
                result.output = f"Interpreter not available: {e}"
        else:
            interpreter = sys.executable if is_python else 'Rscript'
            cmd = [interpreter, script_path]
            popen_kwargs = {
                'cwd': run_dir,
                'env': _safe_env(run_dir),
                'stdout': subprocess.PIPE,
                'stderr': subprocess.PIPE,
                'text': True,
            }
            preexec = _posix_limits(memory_mb)
            if preexec:
                popen_kwargs['preexec_fn'] = preexec
            if os.name == 'posix':
                # New session -> the child leads its own process group, so a
                # timeout can kill the whole tree (grandchildren included), not
                # just the direct child. POSIX-only; unchanged on Windows.
                popen_kwargs['start_new_session'] = True

            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
            except FileNotFoundError as e:
                result.returncode = -1
                result.output = f"Interpreter not available: {e}"
            else:
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                    result.returncode = proc.returncode
                    result.output = _combine_output(
                        stdout, stderr, output_limit)
                except subprocess.TimeoutExpired:
                    _kill_subprocess(proc)
                    # Reap the killed child; what it wrote before the kill is
                    # still in the pipes and is kept (S5).
                    stdout, stderr = proc.communicate()
                    result.timed_out = True
                    result.returncode = -1
                    result.output = _timeout_output(
                        stdout, stderr, timeout, output_limit)

        plot_bytes = skipped = 0
        for plot_path in sorted(glob.glob(os.path.join(run_dir, 'plot*.png'))):
            try:
                size = os.path.getsize(plot_path)
                if (len(result.plots) >= PLOTS_MAX
                        or plot_bytes + size > PLOTS_BYTES_MAX):
                    skipped += 1
                    continue
                with open(plot_path, 'rb') as img:
                    data = img.read(PLOTS_BYTES_MAX - plot_bytes + 1)
                if plot_bytes + len(data) > PLOTS_BYTES_MAX:
                    skipped += 1     # grew while being read
                    continue
                plot_bytes += len(data)
                result.plots.append(base64.b64encode(data).decode('utf-8'))
            except OSError:
                logger.warning("Could not read plot file %s", plot_path)
        if skipped:
            result.output = (result.output or '') + plot_limit_note(skipped)

        for name in collect:
            path = os.path.join(run_dir, name)
            if os.path.exists(path):
                try:
                    with open(path, 'rb') as f:
                        result.files[name] = f.read()
                except OSError:
                    logger.warning("Could not collect output file %s", name)
        return result
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
