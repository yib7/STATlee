"""Methods and assumptions card (SP3).

The draft prompt asks every generated script (Python or R) to finish its
output with a fixed block::

    == METHOD ==
    test: <name>; outcome: <var>; predictors: <vars>; n_used: <int>; dropped: <int> (<reason>)
    == ASSUMPTIONS ==
    <check name>: <statistic/result> -> PASS|WARN|FAIL
    == EFFECT ==
    <effect size name> = <value> [95% CI <lo>, <hi>]
    == END ==

``parse`` turns that block into a small card::

    {"method": {"test", "outcome", "predictors", "n_used", "dropped",
                "dropped_reason"} | None,
     "assumptions": [{"name", "detail", "status"}],   # pass|warn|fail|unknown
     "effects": [{"text"}],
     "reported": bool}

It is lenient (case, spacing, missing sections, extra lines, R ``[1]``
prefixes and quotes, Windows line endings, a repeated block: the last one
wins), bounded (counts and string lengths are clamped so a huge output cannot
bloat the run's meta) and never raises. The text comes from a script the
model wrote, so every value is untrusted: the UI renders it as text only and
prompts carry it inside untrusted-data delimiters.
"""
import logging
import re

logger = logging.getLogger('statlee.methods')

MAX_ASSUMPTIONS = 20
MAX_EFFECTS = 10
FIELD_MAX = 200            # one method field (test, outcome, predictors, ...)
NAME_MAX = 120             # an assumption check's name
DETAIL_MAX = 300           # an assumption check's statistic/result
EFFECT_MAX = 300           # one effect size line
SECTION_LINES_MAX = 200    # lines kept per section
HEADER_MAX = 80            # a longer line is never a header
STATUS_TAIL = 64           # chars at the end of a check that hold its verdict
NOTE_MAX = 120             # a note after "-> VERDICT"; longer is not a verdict
SCAN_MAX = 2 * 1024 * 1024  # chars of output scanned (the block is at the end)

STATUSES = ('pass', 'warn', 'fail', 'unknown')
METHOD_KEYS = ('test', 'outcome', 'predictors', 'n_used', 'dropped',
               'dropped_reason')

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
_CTRL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]')
_R_PREFIX_RE = re.compile(r'^\[\d+\]\s*')
_HEADER_RE = re.compile(
    r'^={2,}\s*(method|methods|assumptions?|assumption\s+checks|effects?|'
    r'effect\s+sizes?|end)\s*={2,}$', re.I)
_SEPARATOR_RE = re.compile(r'^[\s\-=_*#~.]*$')
# The verdict closing a check line, searched in the line's last few chars
# only, so a pathological long line cannot make the regex backtrack.
_STATUS_RE = re.compile(
    r'(?:\s*(?P<arrow>-+>|=+>|\u2192)\s*|\s+)'
    r'[\[(]?(?P<status>pass(?:ed)?|ok|warn(?:ing)?|fail(?:ed)?)[\])]?[.!]?$',
    re.I)
# A bare-word verdict right after a negation ("did not pass", "not ok") is
# not a verdict, also with an article or punctuation in between ("not a
# pass", "no, pass", "cannot pass"). Only checked when no arrow separates the
# verdict: with an arrow ("Outliers: no -> PASS", "cannot reject -> PASS")
# the words before it are the result.
_NEGATION_RE = re.compile(
    r"(?:\bnot|\bno|\bcannot|n['’]t|\bnever|\bfail(?:s|ed)?\s+to|"
    r"\bunable\s+to)(?:[\s,;:]+(?:an?|the)\b)?[\s,;:]*$",
    re.I)
_ARROW_RE = re.compile(r'-+>|=+>|\u2192')
# An arrow, its verdict, then a short note: "-> FAIL (use Welch)".
_ARROW_NOTE_RE = re.compile(
    r'\s*[\[(]?(?P<status>pass(?:ed)?|ok|warn(?:ing)?|fail(?:ed)?)\b[\])]?'
    r'(?P<note>[\s;,:.\-(].*)$', re.I)
# The contract's own placeholders: a line still holding them is an unfilled
# echo of the template, not a result.
_PLACEHOLDER_RE = re.compile(
    r'<(?:name|vars?|int|reason|check name|statistic/result|effect size name|'
    r'value|lo|hi)>|PASS\|WARN\|FAIL', re.I)
# A check the script could not compute ("not computed (<error>) -> WARN", the
# draft prompt's fallback for a diagnostic that raised) is a caveat: it reads
# as WARN even when the line says PASS or gives no verdict. FAIL stays FAIL.
_NOT_COMPUTED_RE = re.compile(
    r"\b(?:not|never)\s+(?:be(?:en)?\s+)?(?:computed|calculated|estimated)\b"
    r"|\b(?:could|can)\s*(?:not|n['’]t)\s+(?:be\s+)?"
    r"(?:compute|calculate|estimate)", re.I)
_STATUS_MAP = {'pass': 'pass', 'passed': 'pass', 'ok': 'pass',
               'warn': 'warn', 'warning': 'warn',
               'fail': 'fail', 'failed': 'fail'}
_KEY_ALIASES = {
    'test': 'test', 'method': 'test', 'model': 'test',
    'outcome': 'outcome', 'dv': 'outcome', 'dependent': 'outcome',
    'predictors': 'predictors', 'predictor': 'predictors', 'iv': 'predictors',
    'ivs': 'predictors', 'independent': 'predictors',
    'n_used': 'n_used', 'n': 'n_used', 'nused': 'n_used', 'n_analysed': 'n_used',
    'n_analyzed': 'n_used',
    'dropped': 'dropped', 'n_dropped': 'dropped',
}
_INT_RE = re.compile(r'^(\d[\d,_]*)(?![\d.])')
_DROPPED_RE = re.compile(r'^(\d[\d,_]*)(?![\d.])\s*(?:\((?P<reason>.*)\))?')


def empty():
    return {'method': None, 'assumptions': [], 'effects': [], 'reported': False}


def _clean(value, limit):
    """One line of plain text: ANSI codes, control characters and lone
    surrogates removed, whitespace collapsed, clamped to ``limit``."""
    text = _ANSI_RE.sub('', str(value))
    text = _CTRL_RE.sub('', text.replace('\t', ' '))
    text = ' '.join(text.split())
    return text[:limit].strip()


def _norm_line(line):
    """Strip one output line of R's ``[1] "..."`` print wrapping."""
    s = line.strip()
    m = _R_PREFIX_RE.match(s)
    if m:
        s = s[m.end():]
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return s.strip()


def _header_kind(line):
    if '=' not in line or len(line) > HEADER_MAX:
        return None
    m = _HEADER_RE.match(line)
    if not m:
        return None
    word = m.group(1).lower()
    if word.startswith('method'):
        return 'method'
    if word.startswith('assumption'):
        return 'assumptions'
    if word.startswith('effect'):
        return 'effects'
    return 'end'


def _last_block(lines):
    """``(start, end)`` line indices of the last block, or None. A block
    starts at a METHOD header, at the first header after an END, or at a
    section seen twice since the last start (a repeated block without its
    METHOD part); it ends at END or at the end of the output."""
    start = None
    open_kinds = None            # None: no open block
    for i, line in enumerate(lines):
        kind = _header_kind(line)
        if kind is None:
            continue
        if kind == 'end':
            open_kinds = None
            continue
        if kind == 'method' or open_kinds is None or kind in open_kinds:
            start = i
            open_kinds = {kind}
        else:
            open_kinds.add(kind)
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        if _header_kind(lines[j]) == 'end':
            return start, j
    return start, len(lines)


def _parse_int(value):
    m = _INT_RE.match(value)
    if not m:
        return None
    try:
        return int(re.sub(r'[,_]', '', m.group(1)))
    except ValueError:
        return None


def _parse_method(lines):
    method = dict.fromkeys(METHOD_KEYS)
    for piece in ';'.join(lines).split(';'):
        if ':' not in piece:
            continue
        key, value = piece.split(':', 1)
        key = re.sub(r'[\s\-]+', '_', key.strip().lower())
        name = _KEY_ALIASES.get(key)
        value = _clean(value, FIELD_MAX)
        # Unknown key, empty or unfilled value: skip. Given twice: first wins.
        if (not name or not value or method[name] is not None
                or _PLACEHOLDER_RE.search(value)):
            continue
        if name == 'n_used':
            method['n_used'] = _parse_int(value)
        elif name == 'dropped':
            m = _DROPPED_RE.match(value)
            if m:
                method['dropped'] = _parse_int(m.group(1))
                reason = _clean(m.group('reason') or '', FIELD_MAX)
                method['dropped_reason'] = reason or None
        else:
            method[name] = value
    if not any(method[k] is not None for k in METHOD_KEYS):
        return None
    return method


def _split_check(body):
    name, sep, detail = body.partition(':')
    name = _clean(name, NAME_MAX).rstrip(' -')
    detail = _clean(detail, DETAIL_MAX).rstrip(' -') if sep else ''
    if not name and detail:
        name, detail = detail[:NAME_MAX], ''
    return name, detail


def _verdict(line):
    """``(body, status, note)`` of one check line. The verdict is the last
    word (after an arrow, or a bare word that is not negated), or an arrow's
    verdict followed by a short note. Anything else is ``unknown``."""
    cut = max(0, len(line) - STATUS_TAIL)
    m = _STATUS_RE.search(line, cut)
    if m:
        body = line[:m.start()]
        if m.group('arrow') is None and _NEGATION_RE.search(body[-40:]):
            return line, 'unknown', ''
        return body, _STATUS_MAP[m.group('status').lower()], ''
    arrows = list(_ARROW_RE.finditer(line))
    for arrow in reversed(arrows[-3:]):
        rest = line[arrow.end():]
        if len(rest) > NOTE_MAX + 16:
            continue
        n = _ARROW_NOTE_RE.match(rest)
        if n:
            note = _clean(n.group('note'), NOTE_MAX).strip(' ;,:.-')
            if len(note) > 1 and note[0] == '(' and note[-1] == ')':
                note = note[1:-1].strip()
            return (line[:arrow.start()],
                    _STATUS_MAP[n.group('status').lower()], note)
    return line, 'unknown', ''


def _parse_assumptions(lines):
    out = []
    for line in lines:
        if len(out) >= MAX_ASSUMPTIONS:
            break
        if _SEPARATOR_RE.match(line) or _PLACEHOLDER_RE.search(line):
            continue
        body, status, note = _verdict(line)
        if status in ('pass', 'unknown') and _NOT_COMPUTED_RE.search(line):
            status = 'warn'
        name, detail = _split_check(body)
        if not name:
            continue
        if note:
            detail = (f'{detail}; {note}' if detail else note)[:DETAIL_MAX]
        out.append({'name': name, 'detail': detail, 'status': status})
    return out


def _parse_effects(lines):
    out = []
    for line in lines:
        if len(out) >= MAX_EFFECTS:
            break
        if _SEPARATOR_RE.match(line) or _PLACEHOLDER_RE.search(line):
            continue
        text = _clean(line, EFFECT_MAX)
        if text:
            out.append({'text': text})
    return out


def _parse(text):
    if len(text) > SCAN_MAX:
        text = text[-SCAN_MAX:]
    lines = [_norm_line(line) for line in text.splitlines()]
    block = _last_block(lines)
    if block is None:
        return empty()
    start, end = block
    sections = {'method': [], 'assumptions': [], 'effects': []}
    current = None
    for line in lines[start:end]:
        kind = _header_kind(line)
        if kind:
            current = kind if kind in sections else None
            continue
        if current and line and len(sections[current]) < SECTION_LINES_MAX:
            sections[current].append(line)
    card = {
        'method': _parse_method(sections['method']),
        'assumptions': _parse_assumptions(sections['assumptions']),
        'effects': _parse_effects(sections['effects']),
    }
    card['reported'] = bool(card['method'] or card['assumptions']
                            or card['effects'])
    return card


def parse(output):
    """The methods card of a run's output. Never raises: anything that is
    not text, or has no block, is an unreported card."""
    if not isinstance(output, str) or not output.strip():
        return empty()
    try:
        return _parse(output)
    except Exception:            # never let a parser bug break a run
        logger.warning("Could not parse the methods block", exc_info=True)
        return empty()


def _norm_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def normalize(card):
    """Re-validate a stored card (run meta is server-written, but a corrupt
    or hand-edited file must not reach the UI or a prompt unbounded).
    Returns None for anything that is not a card."""
    if not isinstance(card, dict):
        return None
    try:
        method = None
        raw = card.get('method')
        if isinstance(raw, dict):
            method = dict.fromkeys(METHOD_KEYS)
            for key in ('test', 'outcome', 'predictors', 'dropped_reason'):
                if isinstance(raw.get(key), str):
                    method[key] = _clean(raw[key], FIELD_MAX) or None
            method['n_used'] = _norm_int(raw.get('n_used'))
            method['dropped'] = _norm_int(raw.get('dropped'))
            if not any(method[k] is not None for k in METHOD_KEYS):
                method = None
        assumptions = []
        for item in card.get('assumptions') or []:
            if len(assumptions) >= MAX_ASSUMPTIONS:
                break
            if not isinstance(item, dict) or item.get('name') is None:
                continue
            name = _clean(item['name'], NAME_MAX)
            if not name:
                continue
            detail = item.get('detail')
            status = item.get('status')
            assumptions.append({
                'name': name,
                'detail': _clean(detail, DETAIL_MAX) if detail is not None else '',
                'status': status if status in STATUSES else 'unknown'})
        effects = []
        for item in card.get('effects') or []:
            if len(effects) >= MAX_EFFECTS:
                break
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                text = _clean(item['text'], EFFECT_MAX)
                if text:
                    effects.append({'text': text})
        return {'method': method, 'assumptions': assumptions,
                'effects': effects,
                'reported': bool(method or assumptions or effects)}
    except Exception:
        logger.warning("Discarding a malformed methods card", exc_info=True)
        return None


def for_run(meta, output):
    """The card of a stored run: its saved card, or (``methods`` null, a run
    saved before SP3) one parsed from its output on the fly. Not written
    back: a read never mutates the run."""
    stored = meta.get('methods') if isinstance(meta, dict) else None
    card = normalize(stored) if isinstance(stored, dict) else None
    return card if card is not None else parse(output)


NOT_REPORTED = ('Assumptions not reported: the script did not print a '
                'methods summary, so no assumption checks, N used or effect '
                'sizes were reported in the standard form.')


def render_text(card):
    """Plain-text card for a prompt. '' when there is no card at all."""
    card = normalize(card)
    if card is None:
        return ''
    if not card['reported']:
        return NOT_REPORTED
    lines = []
    method = card['method']
    if method:
        for key, label in (('test', 'Method'), ('outcome', 'Outcome'),
                           ('predictors', 'Predictors'), ('n_used', 'N used')):
            if method[key] is not None:
                lines.append(f'{label}: {method[key]}')
        if method['dropped'] is not None:
            reason = (f" ({method['dropped_reason']})"
                      if method['dropped_reason'] else '')
            lines.append(f"Dropped: {method['dropped']}{reason}")
    else:
        lines.append('Method: not reported')
    if card['assumptions']:
        lines.append('Assumption checks:')
        for a in card['assumptions']:
            head = f"{a['name']}: {a['detail']}" if a['detail'] else a['name']
            verdict = (a['status'].upper() if a['status'] != 'unknown'
                       else 'no verdict')
            lines.append(f'- {head} -> {verdict}')
    else:
        lines.append('Assumption checks: none reported')
    if card['effects']:
        lines.append('Effect sizes:')
        lines.extend(f"- {e['text']}" for e in card['effects'])
    else:
        lines.append('Effect sizes: none reported')
    return '\n'.join(lines)
