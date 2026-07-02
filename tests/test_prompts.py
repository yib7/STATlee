"""Prompt builders (statlee/prompts.py): history/turn iterators must tolerate
malformed (non-dict) items in a client-supplied history list rather than
raising (P1-5) — the shape guard skips them instead of 500ing the caller."""
from statlee import prompts


def test_draft_skips_non_dict_history_items():
    history = ['oops', {'role': 'user', 'text': 'run a t-test'}]
    prompt = prompts.draft(
        filename='test.csv', headers=['a', 'b'], codebook={},
        language='python', metadata_summary='', history=history,
        user_prompt='go')
    assert 'run a t-test' in prompt


def test_format_turns_skips_non_dict_items():
    turns = ['oops', {'role': 'assistant', 'text': 'the effect is significant'}]
    text = prompts._format_turns(turns)
    assert 'the effect is significant' in text
    assert 'oops' not in text
