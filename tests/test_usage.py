"""Per-model usage aggregation for the session-cost display (3.4)."""
from statlee.usage import usage_breakdown


def test_usage_breakdown_totals_and_by_model():
    b = usage_breakdown(
        {'model': 'm1', 'input': 10, 'output': 5},
        {'model': 'm1', 'input': 1, 'output': 2},
        {'model': 'm2', 'input': 4, 'output': 0},
        None,                       # falsy entries are ignored
    )
    assert b['input'] == 15
    assert b['output'] == 7
    assert b['calls'] == 3
    assert b['by_model']['m1'] == {'input': 11, 'output': 7, 'calls': 2}
    assert b['by_model']['m2'] == {'input': 4, 'output': 0, 'calls': 1}


def test_usage_breakdown_empty():
    b = usage_breakdown()
    assert b == {'input': 0, 'output': 0, 'calls': 0, 'by_model': {}}


def test_usage_breakdown_defaults_missing_model_to_unknown():
    b = usage_breakdown({'input': 3, 'output': 1})
    assert b['by_model'] == {'unknown': {'input': 3, 'output': 1, 'calls': 1}}
