"""Client-facing LLM usage aggregation (3.4 cost display).

Each LLM call returns a per-call usage dict shaped like
``{'model': <id>, 'input': <int>, 'output': <int>}``. ``usage_breakdown``
folds any number of those into the totals the UI needs, *plus* a per-model
split so the session-cost tooltip can price each model separately (the various
models have different per-token rates).
"""


def usage_breakdown(*usages):
    """Aggregate per-call usage dicts into a client-facing summary.

    Falsy entries are skipped. Returns::

        {'input': int, 'output': int, 'calls': int,
         'by_model': {model_id: {'input': int, 'output': int, 'calls': int}}}
    """
    total = {'input': 0, 'output': 0, 'calls': 0, 'by_model': {}}
    for u in usages:
        if not u:
            continue
        in_tok = u.get('input', 0) or 0
        out_tok = u.get('output', 0) or 0
        model = u.get('model') or 'unknown'
        total['input'] += in_tok
        total['output'] += out_tok
        total['calls'] += 1
        entry = total['by_model'].setdefault(
            model, {'input': 0, 'output': 0, 'calls': 0})
        entry['input'] += in_tok
        entry['output'] += out_tok
        entry['calls'] += 1
    return total
