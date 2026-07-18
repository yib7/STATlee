"""Non-LLM static pre-check for generated/edited analysis scripts (P1-1a).

This is DEFENSE IN DEPTH, added to — never replacing — the two LLM moderation
passes in the run-guard. The default ``SANDBOX_MODE=subprocess`` provides no
network or filesystem isolation, so the LLM ``code_moderation`` gate is the only
barrier before execution, and an LLM gate is prompt-injection bypassable. This
module encodes the same prohibitions that ``prompts.code_moderation`` describes
to a model (network access, env/file exfiltration, process spawning, shell
execution, sandbox escape) as deterministic code, so the safety gate is no
longer solely LLM-dependent.

Design:
- Python is parsed with the stdlib ``ast`` and walked. A genuinely dangerous
  construct fails CLOSED (blocked); a script that does not parse fails OPEN on
  purpose (see ``check_code``).
- R cannot be parsed by Python's ``ast``, so it gets a coarser, textual/regex
  denylist. It is best-effort and deliberately conservative.
- The module is self-contained (stdlib ``ast``/``re`` only) and importable with
  no side effects.
"""
import ast
import re

# ---------------------------------------------------------------------------
# Python denylists
# ---------------------------------------------------------------------------

# Top-level module names whose mere import signals network / process / dynamic
# code-loading capability. Matched against the top-level component of the
# imported name (``import x.y`` and ``from x.y import z`` both match ``x``).
#
# NOTE: os, sys, pathlib, shutil, glob are intentionally NOT here. A bare
# ``import os`` / ``import sys`` is harmless and legitimate analysis scripts
# touch ``os.path``; an existing test
# (test_analyze_routes.py::test_run_guard_blocks_malformed_code_moderation)
# depends on ``import os`` reaching the downstream LLM gate. Their DANGEROUS
# ATTRIBUTES are blocked below instead.
_DANGEROUS_MODULES = frozenset({
    'socket', 'ssl', 'urllib', 'urllib2', 'urllib3', 'http', 'httplib',
    'requests', 'aiohttp', 'httpx', 'ftplib', 'telnetlib', 'smtplib',
    'poplib', 'imaplib', 'subprocess', 'multiprocessing', 'ctypes', 'cffi',
    'importlib', 'imp', 'pty', 'pickle', 'shelve', 'marshal', 'dill',
    'joblib', 'code', 'codeop', 'signal', 'resource', 'pdb', 'webbrowser',
    'xmlrpc', 'asyncio',
})

# Dangerous ``os.*`` attributes (exact names). ``os.exec*`` / ``os.spawn*`` are
# matched by prefix separately.
_OS_ATTRS = frozenset({
    'system', 'popen', 'fork', 'forkpty', 'posix_spawn', 'putenv', 'environ',
    'getenv', 'remove', 'unlink', 'rmdir', 'removedirs', 'startfile',
})

# Modules for which ANY attribute access is blocked (they are also on the
# import denylist; this catches access through a name bound some other way).
_WHOLESALE_MODULES = frozenset({'subprocess', 'socket', 'pty', 'importlib'})

# Attribute names used by the classic ``().__class__.__bases__[0].
# __subclasses__()`` sandbox-escape / builtins-reflection idiom. Any attribute
# with one of these names is blocked regardless of what it hangs off.
_DUNDER_ESCAPE = frozenset({
    '__globals__', '__builtins__', '__subclasses__', '__bases__', '__mro__',
    '__import__', '__loader__', '__code__',
})

# Builtins that execute arbitrary code or read stdin. Matched only when called
# as a BARE name (``eval(...)``), never as an attribute — blocking attribute
# form would reject legitimate methods that happen to share a name (e.g. pandas
# ``df.eval(...)`` / ``df.query(...)``). Attribute-based escapes back to the real
# builtins go through ``__builtins__`` / ``__globals__`` / ``__import__``, which
# ARE blocked by the dunder rule above.
_DANGEROUS_BUILTINS = frozenset({
    'eval', 'exec', 'compile', '__import__', 'execfile', 'input', 'breakpoint',
})

# Builtins that perform DYNAMIC attribute access; only blocked when the
# attribute-name argument is not a constant string (a literal like
# ``getattr(df, 'mean')`` is fine).
_DYNAMIC_ATTR_BUILTINS = frozenset({'getattr', 'setattr', 'delattr'})


def _top_module(name):
    """Top-level component of a dotted module name (``a.b.c`` -> ``a``)."""
    return (name or '').split('.', 1)[0]


def _module_attr_reason(mod, attr):
    """Reason string if ``mod.attr`` is a blocked module attribute, else None."""
    if mod == 'os':
        if attr in _OS_ATTRS or attr.startswith('exec') or attr.startswith('spawn'):
            return f'calls a shell/exec/filesystem primitive: os.{attr}'
    elif mod == 'sys':
        if attr == 'modules':
            return 'accesses the module registry: sys.modules'
    elif mod == 'shutil':
        if attr in ('rmtree', 'move'):
            return f'mutates the filesystem: shutil.{attr}'
    elif mod in _WHOLESALE_MODULES:
        return f'uses a network/process primitive: {mod}.{attr}'
    elif mod == 'pickle':
        if attr in ('load', 'loads'):
            return f'deserializes untrusted data: pickle.{attr}'
    return None


def _is_str_constant(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _bad_open_path_reason(path):
    """Reason if the string literal ``path`` is outside the working directory
    (absolute, or contains a ``..`` segment), else None."""
    # Absolute: POSIX ``/…``, UNC / Windows ``\…``, or a drive letter ``C:``.
    if (path.startswith('/') or path.startswith('\\')
            or re.match(r'^[A-Za-z]:', path)):
        return f'opens a file outside the working directory: {path}'
    # Cross-platform ``..`` segment detection (split on both separators).
    segments = re.split(r'[\\/]', path)
    if '..' in segments:
        return f'opens a file outside the working directory: {path}'
    return None


def _check_python(code):
    """Walk the AST and return (blocked, reason). See ``check_code`` for the
    fail-open-on-SyntaxError rationale."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # FAIL-OPEN on syntax errors ON PURPOSE: a script that does not parse
        # cannot execute anything harmful, so let it reach the sandbox and fail
        # safely there. This preserves the /interpret auto-debug flow, which
        # legitimately round-trips broken scripts.
        return False, ''
    except (ValueError, RecursionError, MemoryError):
        # ast.parse can raise ValueError (e.g. null bytes) or blow the recursion
        # limit on pathological input. Nothing executable was proven safe, so
        # fail CLOSED here.
        return True, 'the script could not be statically analyzed'

    for node in ast.walk(tree):
        # --- imports ---------------------------------------------------------
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = _top_module(alias.name)
                if mod in _DANGEROUS_MODULES:
                    return True, f'imports a network/process module: {mod}'
        elif isinstance(node, ast.ImportFrom):
            # node.module is None for ``from . import x`` (relative) -> skip.
            mod = _top_module(node.module) if node.module else ''
            if mod in _DANGEROUS_MODULES:
                return True, f'imports a network/process module: {mod}'

        # --- attribute access ------------------------------------------------
        elif isinstance(node, ast.Attribute):
            if node.attr in _DUNDER_ESCAPE:
                return True, f'uses a sandbox-escape attribute: {node.attr}'
            if isinstance(node.value, ast.Name):
                reason = _module_attr_reason(node.value.id, node.attr)
                if reason:
                    return True, reason

        # --- bare reflective names -------------------------------------------
        # Catches the same escape names used as a BARE reference rather than an
        # attribute, e.g. ``getattr(__builtins__, 'eval')`` or a raw
        # ``__builtins__`` lookup. Legitimate analysis code never references
        # these as bare names, so this is false-positive-free.
        elif isinstance(node, ast.Name):
            if node.id in _DUNDER_ESCAPE:
                return True, f'uses a sandbox-escape name: {node.id}'

        # --- calls -----------------------------------------------------------
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
                if name in _DANGEROUS_BUILTINS:
                    return True, f'calls a dangerous builtin: {name}'
                if name in _DYNAMIC_ATTR_BUILTINS:
                    # args[1] is the attribute name; block only if it is not a
                    # constant string (dynamic access used to defeat static
                    # analysis). getattr(obj, 'literal') is fine.
                    if len(node.args) >= 2 and not _is_str_constant(node.args[1]):
                        return True, (f'uses dynamic attribute access to bypass '
                                      f'static checks: {name}(...)')
                if name == 'open' and node.args:
                    reason = _open_call_reason(node.args[0])
                    if reason:
                        return True, reason
            elif (isinstance(func, ast.Attribute) and func.attr == 'open'
                  and isinstance(func.value, ast.Name)
                  and func.value.id == 'io'):
                # io.open(...) is the same primitive as the open() builtin.
                if node.args:
                    reason = _open_call_reason(node.args[0])
                    if reason:
                        return True, reason

    return False, ''


def _open_call_reason(first_arg):
    """Reason if an open()/io.open() first argument is a disallowed path.

    Only string-CONSTANT first args are inspected. When the first argument is a
    variable or expression we ALLOW it (return None) rather than over-block — a
    known limitation of a best-effort static gate; the LLM moderation pass and
    the sandbox's throwaway working directory are the other layers that cover
    the dynamic case.
    """
    if _is_str_constant(first_arg):
        return _bad_open_path_reason(first_arg.value)
    return None


# ---------------------------------------------------------------------------
# R denylist (textual — coarser than the Python AST checker)
# ---------------------------------------------------------------------------
# Python's ``ast`` cannot parse R, so this is a conservative case-insensitive
# regex scan for network / shell / env primitives. It is intentionally coarser
# and may miss obfuscated variants; it is a best-effort backstop behind the LLM
# gate, not a parser.
_R_PATTERNS = [
    (re.compile(r'\bsystem2\s*\(', re.IGNORECASE), 'system2('),
    (re.compile(r'\bsystem\s*\(', re.IGNORECASE), 'system('),
    (re.compile(r'\bshell\s*\(', re.IGNORECASE), 'shell('),
    (re.compile(r'\bdownload\.file\b', re.IGNORECASE), 'download.file'),
    (re.compile(r'\bsocketConnection\b', re.IGNORECASE), 'socketConnection'),
    (re.compile(r'\burl\s*\(', re.IGNORECASE), 'url('),
    (re.compile(r'\bpipe\s*\(', re.IGNORECASE), 'pipe('),
    (re.compile(r'\bcurl\b', re.IGNORECASE), 'curl'),
    (re.compile(r'\bSys\.getenv\b', re.IGNORECASE), 'Sys.getenv'),
    (re.compile(r'\bSys\.setenv\b', re.IGNORECASE), 'Sys.setenv'),
    (re.compile(r'\binstall\.packages\b', re.IGNORECASE), 'install.packages'),
    (re.compile(r'source\s*\(\s*["\'][^"\']*http', re.IGNORECASE),
     'source(...http)'),
]


def _check_r(code):
    for pattern, token in _R_PATTERNS:
        if pattern.search(code or ''):
            return True, f'R script uses a network/shell/env primitive: {token}'
    return False, ''


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_code(code, language='Python'):
    """Return (blocked, reason). blocked=True means refuse to execute.

    ``reason`` is a short human-readable explanation when blocked, else ''.

    This is a non-LLM, defense-in-depth gate run BEFORE every sandbox execution,
    in ADDITION to (never instead of) the LLM ``code_moderation`` pass.
    """
    if not code or not isinstance(code, str):
        return False, ''
    if str(language).strip().lower() == 'python':
        return _check_python(code)
    return _check_r(code)
