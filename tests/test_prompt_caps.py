"""P1-7: server-side caps on client-supplied prompt material.

Every free-text field a client can POST flows into an LLM prompt billed to
the operator's key. These tests pin that oversized payloads are clamped
BEFORE they reach the model - asserted against the prompt text the fake
backend recorded (``fake_llm.calls``) - so a multi-megabyte ``history``
array can never become millions of paid input tokens.
"""
from conftest import SAMPLE_CSV, post_json, sse_events, upload_csv

from statlee.routes import (
    BACKGROUND_MAX,
    FREE_TEXT_MAX,
    HISTORY_FIELD_MAX,
    HISTORY_MAX_TURNS,
    PLOT_B64_MAX,
    PLOTS_MAX_COUNT,
    clamp,
    clamp_codebook,
    clamp_history,
    clamp_plots,
)

# ---------------------------------------------------------------------------
# Helper units
# ---------------------------------------------------------------------------

def test_clamp_truncates_and_coerces():
    assert clamp('abcdef', 3) == 'abc'
    assert clamp('abc', 10) == 'abc'
    assert clamp(None, 10) == ''
    assert clamp(123456, 3) == '123'   # non-str payloads are coerced, then cut


def test_clamp_history_keeps_only_recent_turns_and_truncates_fields():
    turns = [{'role': 'user', 'text': f'turn {i} ' + 'x' * 5000}
             for i in range(HISTORY_MAX_TURNS + 10)]
    clamped = clamp_history(turns)
    assert len(clamped) == HISTORY_MAX_TURNS
    # The oldest turns are the ones dropped; the newest survives.
    assert clamped[-1]['text'].startswith(f'turn {HISTORY_MAX_TURNS + 9} ')
    assert all(len(t['text']) <= HISTORY_FIELD_MAX for t in clamped)


def test_clamp_history_drops_malformed_entries():
    assert clamp_history('not a list') == []
    assert clamp_history(None) == []
    clamped = clamp_history(['just a string', {'text': 'hi'}, 42])
    assert clamped == [{'role': 'user', 'text': 'hi'}]


def test_clamp_codebook_bounds_serialized_size():
    small = {'age': 'Age in years', 'income': 'Household income'}
    assert clamp_codebook(small) == small
    huge = {'age': 'A' * 300000, 'income': 'kept?'}
    clamped = clamp_codebook(huge)
    assert 'age' not in clamped        # single oversized entry exceeds budget
    assert clamp_codebook('not a dict') == {}


def test_clamp_plots_caps_count_and_per_plot_size():
    plots = [f'plot{i}' for i in range(PLOTS_MAX_COUNT + 2)]
    assert clamp_plots(plots) == plots[:PLOTS_MAX_COUNT]
    oversized = 'A' * (PLOT_B64_MAX + 1)
    assert clamp_plots([oversized, 'ok']) == ['ok']   # dropped, not truncated
    assert clamp_plots('not a list') == []
    assert clamp_plots([None, 42, 'ok']) == ['ok']


# ---------------------------------------------------------------------------
# /chat (analyze.py)
# ---------------------------------------------------------------------------

def test_chat_oversized_history_and_code_are_clamped(client, fake_llm):
    upload_csv(client, SAMPLE_CSV)
    history = ([{'role': 'user', 'text': 'OLDEST_TURN_SENTINEL'}]
               + [{'role': 'user', 'text': 'filler'}] * (HISTORY_MAX_TURNS + 5)
               + [{'role': 'user',
                   'text': 'KEPT_TURN' + 'x' * HISTORY_FIELD_MAX
                           + 'TURN_TAIL_SENTINEL'}])
    payload = {
        'filename': 'test.csv', 'prompt': 'summarize',
        'history': history,
        'codebook': {'age': 'A' * 300000 + 'CODEBOOK_TAIL_SENTINEL'},
        'current_code': 'df.head()' + 'y' * FREE_TEXT_MAX
                        + 'CODE_TAIL_SENTINEL',
    }
    resp = post_json(client, '/chat', payload)
    assert resp.status_code == 200
    sse_events(resp)

    draft = next(c for c in fake_llm.calls if c[1] == 'draft')[2]
    assert 'KEPT_TURN' in draft                    # recent turn survived
    assert 'OLDEST_TURN_SENTINEL' not in draft     # old turns dropped
    assert 'TURN_TAIL_SENTINEL' not in draft       # per-field truncation
    assert 'CODE_TAIL_SENTINEL' not in draft       # current_code capped
    assert 'CODEBOOK_TAIL_SENTINEL' not in draft   # codebook budget applied


# ---------------------------------------------------------------------------
# /converse (converse.py)
# ---------------------------------------------------------------------------

def test_converse_oversized_fields_are_clamped(client, fake_llm):
    history = ([{'role': 'user', 'text': 'OLDEST_TURN_SENTINEL'}]
               + [{'role': 'user', 'text': 'filler'}] * (HISTORY_MAX_TURNS + 5))
    payload = {
        'message': 'explain this ' + 'm' * FREE_TEXT_MAX + 'MSG_TAIL_SENTINEL',
        'history': history,
        'context': 'c' * FREE_TEXT_MAX + 'CTX_TAIL_SENTINEL',
        'code': 'z' * FREE_TEXT_MAX + 'CODE_TAIL_SENTINEL',
    }
    resp = post_json(client, '/converse', payload)
    assert resp.status_code == 200
    sse_events(resp)

    prompt = next(c for c in fake_llm.calls if c[1] == 'converse')[2]
    assert 'MSG_TAIL_SENTINEL' not in prompt
    assert 'CTX_TAIL_SENTINEL' not in prompt
    assert 'CODE_TAIL_SENTINEL' not in prompt
    assert 'OLDEST_TURN_SENTINEL' not in prompt
    # The moderation gate must also see the clamped message, not the raw one.
    mod = next(c for c in fake_llm.calls if c[1] == 'moderation')[2]
    assert 'MSG_TAIL_SENTINEL' not in mod


# ---------------------------------------------------------------------------
# /interpret (analyze.py)
# ---------------------------------------------------------------------------

def test_interpret_oversized_output_and_code_are_clamped(client, fake_llm):
    payload = {
        'output': 'Traceback (most recent call last): boom '
                  + 'o' * FREE_TEXT_MAX + 'OUT_TAIL_SENTINEL',
        'code': 'print(x)' + 'q' * FREE_TEXT_MAX + 'CODE_TAIL_SENTINEL',
        'success': False,
    }
    resp = post_json(client, '/interpret', payload)
    assert resp.status_code == 200
    sse_events(resp)

    prompt = next(c for c in fake_llm.calls if c[1] == 'interpret_debug')[2]
    assert 'OUT_TAIL_SENTINEL' not in prompt
    assert 'CODE_TAIL_SENTINEL' not in prompt


def test_interpret_drops_oversized_plots(client, fake_llm):
    # An oversized base64 blob must be dropped server-side; the request still
    # succeeds on the (clamped) output text alone.
    payload = {
        'output': 'p = 0.03',
        'success': True,
        'plots': ['A' * (PLOT_B64_MAX + 1)],
    }
    resp = post_json(client, '/interpret', payload)
    assert resp.status_code == 200
    events = sse_events(resp)
    assert any(e.get('type') == 'done' for e in events)
    # The streamed call carried only the text prompt - no multi-megabyte
    # attachment sneaked into the flattened contents.
    prompt = next(c for c in fake_llm.calls if c[1] == 'interpret')[2]
    assert len(prompt) < PLOT_B64_MAX


# ---------------------------------------------------------------------------
# /generate_report (misc.py) - caps side of P1-2
# ---------------------------------------------------------------------------

def test_generate_report_oversized_fields_are_clamped(client, fake_llm):
    history = ([{'role': 'user', 'text': 'OLDEST_TURN_SENTINEL'}]
               + [{'role': 'user', 'text': 'filler'}] * (HISTORY_MAX_TURNS + 5))
    payload = {
        'output': 'coef=1.2 ' + 'o' * FREE_TEXT_MAX + 'OUT_TAIL_SENTINEL',
        'interpretation': 'significant ' + 'i' * FREE_TEXT_MAX
                          + 'INTERP_TAIL_SENTINEL',
        'background': 'b' * BACKGROUND_MAX + 'BG_TAIL_SENTINEL',
        'history': history,
        'converse': history,
    }
    resp = post_json(client, '/generate_report', payload)
    assert resp.status_code == 200
    sse_events(resp)

    draft = next(c for c in fake_llm.calls if c[1] == 'report_draft')[2]
    assert 'OUT_TAIL_SENTINEL' not in draft
    assert 'INTERP_TAIL_SENTINEL' not in draft
    assert 'BG_TAIL_SENTINEL' not in draft
    assert 'OLDEST_TURN_SENTINEL' not in draft
    final = next(c for c in fake_llm.calls if c[1] == 'report')[2]
    assert 'OUT_TAIL_SENTINEL' not in final
    assert 'BG_TAIL_SENTINEL' not in final
