"""Non-LLM static pre-check for generated/edited analysis scripts (P1-1a).

This is DEFENSE IN DEPTH, added to (never replacing) the two LLM moderation
passes in the run-guard. The default ``SANDBOX_MODE=subprocess`` provides no
network or filesystem isolation, so without this module the LLM
``code_moderation`` gate would be the only barrier before execution, and an LLM
gate is prompt-injection bypassable. This module encodes the prohibitions that
``prompts.code_moderation`` describes to a model (network access, env/file
exfiltration, process spawning, shell execution, sandbox escape) as
deterministic code, so the safety gate is not solely LLM-dependent.

Python model (allowlist + targeted rules), parsed with the stdlib ``ast``:

1. Imports are an ALLOWLIST. Only the top-level modules in
   ``ALLOWED_PYTHON_MODULES`` (the analysis stack plus side-effect-free stdlib)
   may be imported; everything else (``builtins``, ``nt``/``posix``,
   ``subprocess``, ``socket``, ``importlib``, ``runpy``, ``pickle``, ...) is
   refused. Relative imports are refused.
2. Code-execution / namespace builtins (``eval``, ``exec``, ``compile``,
   ``__import__``, ``execfile``, ``breakpoint``, ``globals``, ``locals``,
   ``vars``) are refused wherever their NAME appears (call, rebinding such as
   ``f = exec``, or ``from X import exec``); ``input`` is refused when called.
3. A few allowlisted modules (``os``, ``sys``, ``shutil``) expose dangerous
   members. Those members are refused however the module is reached: directly,
   via ``import ... as`` / ``from ... import`` / plain ``name = module``
   aliases, via a constant ``getattr``, or through an attribute chain on
   another module (``pd.io.common.os.system`` resolves its owner to ``os``).
   Owners that are never legitimate (``builtins``, ``subprocess``, ``socket``,
   ``importlib``, ``ctypes``, ``multiprocessing``, ``runpy``, ...) are refused
   wholesale. Every OTHER use of a host module object as a value is refused,
   because it would bind the module to a name the alias tracking cannot
   follow: walrus, tuple unpacking, a default or lambda argument, a loop
   variable, a container element, an annotated assignment, ``with ... as``, a
   call argument. Only ``module.member``, a call, the first argument of
   ``getattr``/``hasattr``/``setattr``/``delattr`` and a plain
   ``name = module`` (tracked) may touch it.
4. Reflection that defeats the rules above is refused: the classic dunder /
   frame / bound-method escape attributes (``__globals__``, ``__subclasses__``,
   ``__dict__``, ``__self__``, ``f_globals``, ``tb_frame``, ...), non-constant
   ``getattr``/``setattr``/``delattr``, constant ones naming an escape attribute
   or a dangerous builtin, and ``operator.attrgetter``/``methodcaller``.
5. File I/O through the libraries (pandas ``read_*``/``to_*``, numpy
   ``load``/``save``/``loadtxt``..., ``savefig``, ``pathlib.Path(...)``,
   ``os``/``shutil``/``glob`` calls, ``open``) is refused when a CONSTANT path
   (or the constant prefix of an f-string / concatenation) is absolute, uses a
   ``..`` segment, starts with ``~``, or is a URL. Pickle deserialization
   (``to_pickle``/``read_pickle``, ``allow_pickle=True``) is refused outright.

Fail modes: a dangerous construct fails CLOSED (blocked); a script that does not
parse fails OPEN on purpose (see ``_check_python``).

Known limits (best-effort by design): paths built at runtime from variables are
not resolved, and internals of allowlisted third-party libraries are only
covered where an attribute chain names a dangerous owner/member. The LLM
moderation pass and the sandbox's throwaway working directory remain the other
layers for those cases.

R cannot be parsed by Python's ``ast``, so it gets a coarser regex denylist
(``_R_PATTERNS``); it is a backstop, not a parser.

The module is self-contained (stdlib ``ast``/``re`` only) and importable with
no side effects.
"""
import ast
import re

# ---------------------------------------------------------------------------
# Python: import allowlist
# ---------------------------------------------------------------------------

# Top-level module names a generated analysis script may import. Matched
# against the top-level component (``import scipy.stats`` -> ``scipy``).
# os/sys/shutil/pathlib/glob/io are allowed because analysis code legitimately
# uses ``os.path``, ``sys.version``, ``Path('x').exists()`` etc.; their
# dangerous members are refused by the attribute rules below.
ALLOWED_PYTHON_MODULES = frozenset({
    # analysis stack
    'pandas', 'numpy', 'scipy', 'statsmodels', 'matplotlib', 'seaborn',
    'sklearn', 'patsy', 'pingouin', 'tabulate', 'openpyxl', 'xlrd',
    'pyreadstat', 'lifelines', 'linearmodels', 'factor_analyzer', 'semopy',
    'scikit_posthocs',
    # numeric / text stdlib
    'math', 'cmath', 'statistics', 'decimal', 'fractions', 'random', 'json',
    'csv', 're', 'string', 'textwrap', 'unicodedata', 'datetime', 'time',
    'calendar', 'zoneinfo',
    # data-structure / language stdlib
    'collections', 'itertools', 'functools', 'operator', 'heapq', 'bisect',
    'copy', 'pprint', 'numbers', 'typing', 'dataclasses', 'enum', 'abc',
    'warnings', 'contextlib', 'traceback', '__future__',
    # host modules (members policed below)
    'io', 'os', 'sys', 'shutil', 'pathlib', 'glob',
})

# ---------------------------------------------------------------------------
# Python: names, members, and attributes
# ---------------------------------------------------------------------------

# Builtins that execute code or expose a namespace dict. Refused wherever the
# NAME appears (any ast context), so ``f = exec`` cannot launder them. Attribute
# forms with the same spelling (pandas ``df.eval``) are a different node type
# and stay allowed; the escape back to the real builtins goes through
# ``builtins`` / ``__builtins__`` / ``__self__``, which are refused separately.
_DANGEROUS_NAMES = frozenset({
    'eval', 'exec', 'compile', '__import__', 'execfile', 'breakpoint',
    'globals', 'locals', 'vars',
})

# Builtins refused only when CALLED (reading stdin hangs the sandbox).
_CALL_ONLY_BUILTINS = frozenset({'input'})

# Attribute names used by the dunder / frame / bound-method sandbox-escape
# idioms (``().__class__.__bases__[0].__subclasses__()``,
# ``print.__self__.exec``, ``e.__traceback__.tb_frame.f_globals``). Refused
# regardless of what they hang off; also refused as bare names.
_ESCAPE_ATTRS = frozenset({
    '__globals__', '__builtins__', '__subclasses__', '__bases__', '__mro__',
    '__import__', '__loader__', '__code__', '__dict__', '__self__',
    '__func__', '__getattribute__', '__reduce__', '__reduce_ex__',
    '__closure__', '__spec__',
    'f_globals', 'f_builtins', 'f_locals', 'f_back',
    'tb_frame', 'gi_frame', 'cr_frame', 'ag_frame',
})

# Member names that are a network/process/loader/reflection primitive no matter
# which (possibly allowlisted, possibly third-party) module re-exports them,
# e.g. ``pd.io.common.urlopen``. Spelled so they never collide with a dataset
# column name.
_ANY_OWNER_ATTRS = frozenset({
    'attrgetter', 'methodcaller', 'Popen', 'urlopen', 'urlretrieve',
    'check_output', 'check_call', 'getoutput', 'getstatusoutput',
    'create_connection', 'import_module', 'run_path', 'run_module',
    'get_type_hints', 'ForwardRef',
})

# Builtins that perform dynamic attribute access. A non-constant attribute name
# is refused; a constant one is resolved like an ordinary attribute.
_DYNAMIC_ATTR_BUILTINS = frozenset({'getattr', 'setattr', 'delattr'})

# ``from X import *`` is refused for these: it would bind dangerous members as
# bare names the attribute rules never see.
_STAR_IMPORT_BLOCKED = frozenset({'os', 'sys', 'shutil', 'pathlib', 'io'})

# ``os``-equivalent owners (``nt``/``posix`` are what ``os`` wraps).
_OS_LIKE = frozenset({'os', 'nt', 'posix'})

# Dangerous ``os.*`` members (exact names). ``exec*`` / ``spawn*`` are matched
# by prefix separately.
_OS_ATTRS = frozenset({
    'system', 'popen', 'fork', 'forkpty', 'posix_spawn', 'putenv', 'environ',
    'getenv', 'remove', 'unlink', 'rmdir', 'removedirs', 'startfile',
    # Signals (``os.kill(os.getppid(), 9)`` would kill the web worker in
    # subprocess mode, same uid) and filesystem mutations that plant links,
    # FIFOs or device nodes, or rewrite files the analysis did not create.
    'kill', 'killpg', 'mkfifo', 'mknod', 'symlink', 'link', 'chmod', 'chown',
    'lchown', 'rename', 'renames', 'replace', 'truncate',
})

_SYS_ATTRS = frozenset({'modules', '_getframe', '_current_frames'})

# Owners for which ANY member access is refused. None of these is importable
# (allowlist), so this covers reaching them through an alias or attribute chain.
_WHOLESALE_MODULES = frozenset({
    'builtins', 'subprocess', 'socket', 'importlib', 'ctypes',
    'multiprocessing', 'runpy', 'pty', '_socket', '_posixsubprocess',
    '_ctypes', '_ssl',
})

# Owners whose calls get every constant string argument path-checked.
_PATH_CHECKED_OWNERS = _OS_LIKE | frozenset({'shutil', 'glob'})

# Every owner ``_module_attr_reason`` / the path check has a rule for. A local
# name is only tracked as a module alias when it resolves to one of these, so
# alias tracking never over-blocks a non-module name.
_ATTR_RULE_MODULES = (_OS_LIKE | _WHOLESALE_MODULES | _PATH_CHECKED_OWNERS
                      | frozenset({'sys', 'pickle'}))

# Calls that may take a host module as their FIRST argument: the constant-name
# rules in ``_dynamic_attr_reason`` police getattr/setattr/delattr, and
# hasattr only answers a bool.
_MODULE_ARG_CALLS = _DYNAMIC_ATTR_BUILTINS | frozenset({'hasattr'})

# ---------------------------------------------------------------------------
# Python: library file I/O
# ---------------------------------------------------------------------------

_PICKLE_METHODS = frozenset({'to_pickle', 'read_pickle'})

# Attribute calls whose path argument is checked (in addition to every
# ``to_*`` / ``read_*`` method).
_IO_METHODS = frozenset({
    'savefig', 'loadtxt', 'genfromtxt', 'fromfile', 'savetxt', 'tofile',
    'save', 'savez', 'savez_compressed', 'load', 'read_text', 'write_text',
    'read_bytes', 'write_bytes', 'ExcelWriter', 'ExcelFile', 'HDFStore',
    'PdfPages',
})

# Keyword names that carry a path for the I/O methods above.
_PATH_KWARGS = frozenset({
    'path', 'path_or_buf', 'filepath_or_buffer', 'fname', 'file', 'io',
})

# pathlib constructors (called as a bare name or ``pathlib.Path``).
_PATH_CTORS = frozenset({
    'Path', 'PurePath', 'PosixPath', 'WindowsPath', 'PurePosixPath',
    'PureWindowsPath',
})

_URL_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*://')


def _top_module(name):
    """Top-level component of a dotted module name (``a.b.c`` -> ``a``)."""
    return (name or '').split('.', 1)[0]


def _module_attr_reason(mod, attr):
    """Reason string if ``mod.attr`` is a blocked module member, else None."""
    if mod in _OS_LIKE:
        if attr in _OS_ATTRS or attr.startswith('exec') or attr.startswith('spawn'):
            return f'calls a shell/exec/filesystem primitive: {mod}.{attr}'
    elif mod == 'sys':
        if attr in _SYS_ATTRS:
            return f'accesses interpreter internals: sys.{attr}'
    elif mod == 'shutil':
        if attr in ('rmtree', 'move'):
            return f'mutates the filesystem: shutil.{attr}'
    elif mod in _WHOLESALE_MODULES:
        return f'uses a network/process/loader primitive: {mod}.{attr}'
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


def _bad_io_target_reason(path):
    """``_bad_open_path_reason`` plus URL and home-directory targets, which the
    I/O libraries (pandas/numpy/pathlib) resolve but ``open`` alone would not."""
    if _URL_RE.match(path):
        return f'reads or writes a remote URL: {path}'
    if path.startswith('~'):
        return f'opens a file outside the working directory: {path}'
    return _bad_open_path_reason(path)


def _const_prefix(node):
    """Leading constant string of a path expression, or None.

    Handles a plain literal, an f-string whose first piece is literal, a ``+``
    concatenation (left side), and ``os.path.join(<literal>, ...)``. Anything
    else (a variable, a call result) is dynamic and returns None.
    """
    if _is_str_constant(node):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        return _const_prefix(node.values[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _const_prefix(node.left)
    if (isinstance(node, ast.Call) and node.args
            and isinstance(node.func, ast.Attribute) and node.func.attr == 'join'
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == 'path'):
        return _const_prefix(node.args[0])
    return None


def _path_arg_reason(node):
    prefix = _const_prefix(node)
    if prefix is None:
        return None
    return _bad_io_target_reason(prefix)


def _first_path_reason(call):
    """Check a call's first positional arg and its path-like keywords."""
    candidates = list(call.args[:1])
    candidates += [kw.value for kw in call.keywords if kw.arg in _PATH_KWARGS]
    for arg in candidates:
        reason = _path_arg_reason(arg)
        if reason:
            return reason
    return None


def _all_path_reason(call):
    """Check every positional and keyword arg of a call (os/shutil/glob)."""
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        reason = _path_arg_reason(arg)
        if reason:
            return reason
    return None


def _owner_of(node, aliases):
    """Owner name a member access resolves against.

    ``os`` for ``os.x`` / ``o.x`` (aliased); for a chain, the INNER attribute's
    name (``pd.io.common.os.system`` -> ``os``, ``os.sys.modules`` -> ``sys``),
    so reaching a module through another module's attribute cannot dodge its
    rule. None for anything else (a call result, a subscript, ...).
    """
    if isinstance(node, ast.Name):
        if node.id in aliases:
            return aliases[node.id]
        # ``nt``/``posix`` cannot be imported (allowlist), so an unbound name
        # spelled that way is an ordinary variable (``nt.rename(...)`` on a
        # DataFrame), not the os module.
        return None if node.id in ('nt', 'posix') else node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _collect_bindings(tree):
    """Map local names to the module (``aliases``) or module function
    (``functions``: name -> owner module) they are bound to, plus the set of
    every name an import statement binds (``imported``)."""
    aliases = {}
    functions = {}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = (alias.asname or alias.name).split('.', 1)[0]
                aliases[bound] = _top_module(alias.name)
                imported.add(bound)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mod = _top_module(node.module)
            for alias in node.names:
                bound = alias.asname or alias.name
                imported.add(bound)
                # ``from os import sys as s`` binds a module; ``from glob
                # import glob`` binds the module's same-named function.
                if (alias.name in _ATTR_RULE_MODULES
                        and alias.name != node.module.rsplit('.', 1)[-1]):
                    aliases[bound] = alias.name
                elif node.module in _PATH_CHECKED_OWNERS:
                    # ``from shutil import copy`` / ``from glob import glob``
                    # binds a path-taking function.
                    functions[bound] = mod

    # Follow plain ``name = <module>`` rebindings (``o = os``), including chains
    # (``a = os; b = a``), to a fixpoint over the small edge list. Only names
    # resolving to a module the rules care about are bound, so ``x = df`` never
    # over-blocks.
    assign_edges = [
        (tgt.id, node.value.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
        for tgt in node.targets if isinstance(tgt, ast.Name)
    ]
    changed = True
    while changed:
        changed = False
        for tgt_id, src_id in assign_edges:
            rhs = aliases.get(src_id, src_id)
            if rhs in _ATTR_RULE_MODULES and aliases.get(tgt_id) != rhs:
                aliases[tgt_id] = rhs
                changed = True

    # ``c = shutil.copy`` binds a path-taking host function to a bare name.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)):
            owner = aliases.get(node.value.value.id, node.value.value.id)
            if owner in _PATH_CHECKED_OWNERS:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        functions[tgt.id] = owner
    return aliases, functions, imported


def _module_value_reason(node, parent, aliases, functions, imported):
    """Reason if ``node`` uses a host module object as a VALUE (B-04).

    ``node`` is a module when it is a Name bound to one (an import or a
    tracked ``name = module``), or an Attribute naming one on an imported
    module (``pd.io.common.os``). Such a node may only own a member access, be
    called, be the first argument of getattr/hasattr/setattr/delattr, or be
    the right side of a plain ``name = module`` (which the alias tracking
    follows). Any other use would launder the module past the member rules."""
    if isinstance(node, ast.Name):
        if (not isinstance(node.ctx, ast.Load) or node.id in functions
                or aliases.get(node.id) not in _ATTR_RULE_MODULES):
            return None
        label = node.id
    elif isinstance(node, ast.Attribute):
        if (not isinstance(node.ctx, ast.Load)
                or node.attr not in _ATTR_RULE_MODULES
                # ``glob.glob`` is the module's function, not a module.
                or _owner_of(node.value, aliases) == node.attr):
            return None
        root = node.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if not (isinstance(root, ast.Name) and root.id in imported):
            return None
        label = node.attr
    else:
        return None

    if isinstance(parent, ast.Attribute) and parent.value is node:
        return None
    if isinstance(parent, ast.Call):
        if parent.func is node:
            return None
        if (isinstance(parent.func, ast.Name)
                and parent.func.id in _MODULE_ARG_CALLS
                and parent.args and parent.args[0] is node):
            return None
    if (isinstance(node, ast.Name) and isinstance(parent, ast.Assign)
            and parent.value is node
            and all(isinstance(t, ast.Name) for t in parent.targets)):
        return None
    return f'passes a host module around as a value: {label}'


def _import_reason(node):
    if isinstance(node, ast.Import):
        for alias in node.names:
            mod = _top_module(alias.name)
            if mod not in ALLOWED_PYTHON_MODULES:
                return f'imports a module outside the analysis allowlist: {mod}'
        return None

    # ast.ImportFrom
    if node.level or not node.module:
        return 'uses a relative import'
    mod = _top_module(node.module)
    if mod not in ALLOWED_PYTHON_MODULES:
        return f'imports a module outside the analysis allowlist: {mod}'
    for alias in node.names:
        name = alias.name
        if name == '*':
            if mod in _STAR_IMPORT_BLOCKED:
                return f'star-imports a host module: from {node.module} import *'
            continue
        if name in _DANGEROUS_NAMES or name in _ESCAPE_ATTRS:
            return f'imports a dangerous builtin: {name}'
        if name in _ANY_OWNER_ATTRS:
            return f'imports a reflection/process primitive: {name}'
        # ``from os import system`` binds the primitive as a bare name.
        reason = _module_attr_reason(node.module, name)
        if reason:
            return reason
    return None


def _dynamic_attr_reason(node, name, aliases):
    """getattr/setattr/delattr: refuse non-constant names, escape/dangerous
    constant names, and constant names resolving to a blocked module member."""
    if len(node.args) < 2:
        return None
    attr_node = node.args[1]
    if not _is_str_constant(attr_node):
        return (f'uses dynamic attribute access to bypass static checks: '
                f'{name}(...)')
    attr = attr_node.value
    if attr in _ESCAPE_ATTRS or attr in _DANGEROUS_NAMES:
        return f'reaches a sandbox-escape attribute via {name}: {attr}'
    if attr in _ANY_OWNER_ATTRS:
        return f'reaches a reflection/process primitive via {name}: {attr}'
    owner = _owner_of(node.args[0], aliases)
    if owner:
        return _module_attr_reason(owner, attr)
    return None


def _call_reason(node, aliases, functions):
    func = node.func

    # Pickle deserialization switch on any call (numpy ``load``, ...).
    for kw in node.keywords:
        if kw.arg == 'allow_pickle' and not (
                isinstance(kw.value, ast.Constant) and kw.value.value is False):
            return 'enables pickle deserialization: allow_pickle'

    if isinstance(func, ast.Name):
        name = func.id
        if name in _CALL_ONLY_BUILTINS:
            return f'calls a dangerous builtin: {name}'
        if name in _DYNAMIC_ATTR_BUILTINS:
            return _dynamic_attr_reason(node, name, aliases)
        if name == 'open' and node.args:
            return _path_arg_reason(node.args[0])
        if name in _PATH_CTORS:
            return _first_path_reason(node)
        if name in functions:
            return _all_path_reason(node)
        return None

    if isinstance(func, ast.Attribute):
        attr = func.attr
        owner = _owner_of(func.value, aliases)
        if attr in _PICKLE_METHODS:
            return f'deserializes or writes pickle data: {attr}'
        if attr == 'open' and owner == 'io' and node.args:
            # io.open(...) is the same primitive as the open() builtin.
            return _path_arg_reason(node.args[0])
        if owner in _PATH_CHECKED_OWNERS:
            return _all_path_reason(node)
        if (attr in _IO_METHODS or attr in _PATH_CTORS
                or attr.startswith('to_') or attr.startswith('read_')):
            return _first_path_reason(node)
    return None


def _check_python(code):
    """Walk the AST and return (blocked, reason)."""
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

    aliases, functions, imported = _collect_bindings(tree)
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}

    for node in ast.walk(tree):
        reason = None
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            reason = _import_reason(node)

        elif isinstance(node, ast.Attribute):
            if node.attr in _ESCAPE_ATTRS:
                reason = f'uses a sandbox-escape attribute: {node.attr}'
            elif node.attr in _ANY_OWNER_ATTRS:
                reason = f'uses a reflection/process primitive: {node.attr}'
            else:
                owner = _owner_of(node.value, aliases)
                if owner:
                    reason = _module_attr_reason(owner, node.attr)
            reason = reason or _module_value_reason(
                node, parents.get(node), aliases, functions, imported)

        elif isinstance(node, ast.Name):
            # Any context: a call, a rebinding (``f = exec``), an argument.
            if node.id in _DANGEROUS_NAMES:
                reason = f'references a dangerous builtin: {node.id}'
            elif node.id in _ESCAPE_ATTRS:
                reason = f'uses a sandbox-escape name: {node.id}'
            else:
                reason = _module_value_reason(
                    node, parents.get(node), aliases, functions, imported)

        elif isinstance(node, ast.Call):
            reason = _call_reason(node, aliases, functions)

        if reason:
            return True, reason

    return False, ''


# ---------------------------------------------------------------------------
# R denylist (textual; coarser than the Python AST checker)
# ---------------------------------------------------------------------------
# Python's ``ast`` cannot parse R, so this is a regex scan for network / shell /
# env / dynamic-dispatch primitives. It may miss obfuscated variants; it is a
# best-effort backstop behind the LLM gate, not a parser.
_R_DANGEROUS_FN = r'(?:system2?|shell(?:\.exec)?|Sys\.(?:getenv|setenv))'

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
    # Native-code entry points (case-sensitive so ``match.call()`` is fine).
    (re.compile(r'(?<![\w.])\.Call\s*\('), '.Call('),
    (re.compile(r'(?<![\w.])\.External\s*\('), '.External('),
    # Dynamic evaluation / lookup of a dangerous function by name.
    (re.compile(r'\beval\s*\(\s*parse\b', re.IGNORECASE), 'eval(parse(...))'),
    (re.compile(r'\b(?:get0?|mget|match\.fun)\s*\(\s*["\']' + _R_DANGEROUS_FN
                + r'["\']', re.IGNORECASE), 'get("<shell/env function>")'),
    # Network client packages, attached or called by namespace.
    (re.compile(r'\b(?:library|require|requireNamespace)\s*\(\s*["\']?'
                r'(?:httr2?|RCurl|curl|socket)\b', re.IGNORECASE),
     'library(<network package>)'),
    (re.compile(r'\b(?:httr2?|RCurl)::', re.IGNORECASE),
     '<network package>::'),
]

# ``do.call(<fn>, args)``: allowed only when <fn> is a plain (optionally quoted)
# function name that is not itself a shell/env/eval/lookup primitive, so the
# common ``do.call(rbind, lst)`` idiom keeps working while
# ``do.call("system", ...)`` and ``do.call(paste0("sys", "tem"), ...)`` do not.
_R_DO_CALL = re.compile(r'\bdo\.call\s*\(\s*(?:what\s*=\s*)?', re.IGNORECASE)
_R_PLAIN_FN = re.compile(r'(["\']?)([A-Za-z.][\w.]*)\1\s*[,)]')
_R_DO_CALL_DENY = frozenset(name.lower() for name in (
    'system', 'system2', 'shell', 'shell.exec', 'Sys.getenv', 'Sys.setenv',
    'eval', 'evalq', 'parse', 'get', 'get0', 'mget', 'match.fun', 'do.call',
    'source', 'pipe', 'url', 'download.file', 'socketConnection', 'file',
    'readLines', 'readRDS', 'load', 'library', 'require', 'install.packages',
    '.Call', '.External', 'dyn.load',
))


def _r_do_call_reason(code):
    for match in _R_DO_CALL.finditer(code):
        fn = _R_PLAIN_FN.match(code, match.end())
        if not fn or fn.group(2).lower() in _R_DO_CALL_DENY:
            return 'R script uses dynamic dispatch to a non-literal or dangerous function: do.call'
    return None


def _check_r(code):
    code = code or ''
    for pattern, token in _R_PATTERNS:
        if pattern.search(code):
            return True, f'R script uses a network/shell/env primitive: {token}'
    reason = _r_do_call_reason(code)
    if reason:
        return True, reason
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
