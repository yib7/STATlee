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
- 5.2 All ``plot*.png`` files produced by the run are collected, not just one.
"""
import base64
import glob
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

logger = logging.getLogger('statly.sandbox')


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


def _docker_cmd(image, run_dir, script_name, is_python, memory_mb):
    """Sibling-container invocation (0.3, option A)."""
    interpreter = 'python' if is_python else 'Rscript'
    return [
        'docker', 'run', '--rm',
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
                   runner_image='statly-runner', collect=()):
    """Execute ``code`` in a throwaway directory and collect its artifacts."""
    is_python = (language or 'Python').lower() == 'python'
    run_dir = tempfile.mkdtemp(prefix='ccrun_')
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
            cmd = _docker_cmd(runner_image, run_dir, script_name, is_python, memory_mb)
            popen_kwargs = {}
        else:
            interpreter = sys.executable if is_python else 'Rscript'
            cmd = [interpreter, script_path]
            popen_kwargs = {
                'cwd': run_dir,
                'env': _safe_env(run_dir),
            }
            preexec = _posix_limits(memory_mb)
            if preexec:
                popen_kwargs['preexec_fn'] = preexec

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, **popen_kwargs)
            result.returncode = proc.returncode
            output = proc.stdout or ''
            if proc.stderr:
                output += f"\n--- Output/Warnings ---\n{proc.stderr}"
            result.output = _truncate(output.strip(), output_limit)
        except subprocess.TimeoutExpired:
            result.timed_out = True
            result.returncode = -1
            result.output = (
                f"Execution timed out after {timeout} seconds. "
                "Code took too long to run.")
        except FileNotFoundError as e:
            result.returncode = -1
            result.output = f"Interpreter not available: {e}"

        for plot_path in sorted(glob.glob(os.path.join(run_dir, 'plot*.png'))):
            try:
                with open(plot_path, 'rb') as img:
                    result.plots.append(base64.b64encode(img.read()).decode('utf-8'))
            except OSError:
                logger.warning("Could not read plot file %s", plot_path)

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
