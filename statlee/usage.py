"""Client-facing LLM usage aggregation (3.4 cost display).

Each LLM call returns a per-call usage dict shaped like
``{'model': <id>, 'input': <int>, 'output': <int>}``, optionally with
``thinking`` (reasoning tokens, already inside ``output``), ``cached`` (prompt
tokens served from the provider's cache, already inside ``input``) and
``cache_hit: True`` (served from the app's own deterministic-call cache: free).
``usage_breakdown`` folds any number of those into the totals the UI needs,
*plus* a per-model split so the session-cost tooltip can price each model
separately (the various models have different per-token rates).
"""

_OPTIONAL = ('thinking', 'cached')


def usage_breakdown(*usages):
    """Aggregate per-call usage dicts into a client-facing summary.

    Falsy entries are skipped, and app-cache hits are not billed calls: they
    only bump ``cache_hits`` (D3-11). Returns::

        {'input': int, 'output': int, 'calls': int,
         'by_model': {model_id: {'input': int, 'output': int, 'calls': int}}}

    plus ``thinking`` / ``cached`` (totals and per model) when any call
    reported them (D3-3), and ``cache_hits`` when any call was a cache hit.
    """
    total = {'input': 0, 'output': 0, 'calls': 0, 'by_model': {}}
    for u in usages:
        if not u:
            continue
        if u.get('cache_hit'):
            total['cache_hits'] = total.get('cache_hits', 0) + 1
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
        for key in _OPTIONAL:
            value = u.get(key, 0) or 0
            if value:
                total[key] = total.get(key, 0) + value
                entry[key] = entry.get(key, 0) + value
    return total
