"""SP3 methods card: the prompt contract (Python + R), storage in run meta,
``/run`` and ``GET /runs/<id>`` replies, and the server-side card handed to
``/interpret`` and ``/generate_report``."""
from conftest import (
    SAMPLE_CSV,
    csrf_token,
    post_json,
    sse_events,
    upload_csv,
)

from statlee import methods, prompts, storage

BLOCK_LINES = (
    '== METHOD ==',
    'test: <name>; outcome: <var>; predictors: <vars>; n_used: <int>; '
    'dropped: <int> (<reason>)',
    '== ASSUMPTIONS ==',
    '<check name>: <statistic/result> -> PASS|WARN|FAIL',
    '== EFFECT ==',
    '<effect size name> = <value> [95% CI <lo>, <hi>]',
    '== END ==',
)

BLOCK_OUTPUT = (
    'Group means printed above\n'
    '== METHOD ==\n'
    'test: Welch t-test; outcome: income; predictors: group; n_used: 3; '
    'dropped: 1 (missing income)\n'
    '== ASSUMPTIONS ==\n'
    'Normality (Shapiro-Wilk): W=0.98, p=0.21 -> PASS\n'
    'Equal variances (Levene): p=0.04 -> WARN\n'
    '== EFFECT ==\n'
    "Cohen's d = 0.41 [95% CI 0.12, 0.70]\n"
    '== END ==\n'
)

BLOCK_SCRIPT = 'print(' + repr(BLOCK_OUTPUT) + ')'


def _identity(client):
    csrf_token(client)
    with client.session_transaction() as sess:
        return f"anon_{sess['sid']}"


def _seed(app, client, output, interp=None, **kw):
    ident = _identity(client)
    with app.app_context():
        rid = storage.save_run(output, [], script="print('x')",
                               language='Python', identity=ident,
                               returncode=0, **kw)
        if interp is not None:
            storage.save_interpretation(interp, run_id=rid, identity=ident)
    return rid


def _prompt(fake, kind):
    return next(t for _, k, t in fake.calls if k == kind)


def _inside(prompt, marker, label='METHODS CARD'):
    """Some occurrence of ``marker`` sits inside a <<<BEGIN label ...
    <<<END label>>> block (the raw output elsewhere may repeat it)."""
    i = prompt.find(marker)
    while i != -1:
        begin = prompt.rfind(f'<<<BEGIN {label}', 0, i)
        end = prompt.rfind(f'<<<END {label}>>>', 0, i)
        if begin != -1 and begin > end:
            return True
        i = prompt.find(marker, i + 1)
    return False


def _draft(language):
    return prompts.draft('d.csv', ['a', 'b'], {'a': 'Continuous'}, language,
                         '{"a": {}}', [], 'run a t-test')


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------

def test_python_draft_carries_the_block_contract_verbatim():
    p = _draft('Python')
    for line in BLOCK_LINES:
        assert line in p, line
    assert p.count('== METHOD ==') == 1
    assert 'print() each line' in p
    assert 'cat(' not in p


def test_r_draft_carries_the_block_and_asks_for_cat():
    p = _draft('R')
    for line in BLOCK_LINES:
        assert line in p, line
    assert p.count('== METHOD ==') == 1
    assert 'cat(' in p


def test_rigor_rules_are_folded_into_the_block_not_repeated():
    low = _draft('Python').lower()
    # The old free-form "print the checks / print an effect size" lines now
    # point at the block instead of asking for a second, unstructured copy.
    assert 'print the checks' not in low
    assert 'print an effect size' not in low
    assert 'at the very end' in low
    assert 'never placeholders' in low


def test_validation_keeps_the_block():
    p = prompts.validation('Python', 'd.csv', "print(1)", user_prompt='go')
    assert '== METHOD ==' in p and '== END ==' in p
    assert 'never remove' in p.lower()


def test_interpret_prompt_carries_the_card_inside_delimiters():
    card = methods.render_text(methods.parse(BLOCK_OUTPUT))
    p = prompts.interpret('out', False, methods_card=card)
    assert _inside(p, 'Welch t-test')
    assert _inside(p, 'Equal variances (Levene): p=0.04 -> WARN')
    assert 'cite' in p.lower()
    assert 'METHODS CARD' not in prompts.interpret('out', False)


def test_report_prompts_carry_the_card_inside_delimiters():
    card = 'Method: Welch t-test'
    d = prompts.report_draft('', 'short', 'report', 'out', 'interp', [], [],
                             methods_card=card)
    r = prompts.report('', 'short', 'academic', 'report', 'out', 'interp', [],
                       methods_card=card)
    for p in (d, r):
        assert _inside(p, 'Method: Welch t-test')
        assert 'cite' in p.lower()
    assert 'METHODS CARD' not in prompts.report_draft(
        '', 'short', 'report', 'out', 'interp', [], [])


# ---------------------------------------------------------------------------
# /run stores and returns the card; GET /runs/<id> returns it
# ---------------------------------------------------------------------------

def _generate_and_run(client, fake_llm, code):
    fake_llm.set('validation', code)
    upload_csv(client, SAMPLE_CSV)
    sse_events(post_json(client, '/chat', {'filename': 'test.csv',
                                           'prompt': 'Compare income'}))
    resp = post_json(client, '/run', {'filename': 'test.csv', 'code': code,
                                      'language': 'Python'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def test_run_parses_stores_and_returns_the_card(app, client, fake_llm):
    data = _generate_and_run(client, fake_llm, BLOCK_SCRIPT)
    card = data['methods']
    assert card['reported'] is True
    assert card['method']['test'] == 'Welch t-test'
    assert card['method']['n_used'] == 3
    assert [a['status'] for a in card['assumptions']] == ['pass', 'warn']
    assert card['effects'] == [{'text': "Cohen's d = 0.41 [95% CI 0.12, 0.70]"}]
    # the raw block stays in the terminal output
    assert '== METHOD ==' in data['output']
    ident = _identity(client)
    with app.app_context():
        meta = storage.load_run(data['run_id'], identity=ident)['meta']
    assert meta['methods'] == card
    got = client.get(f"/runs/{data['run_id']}").get_json()
    assert got['methods'] == card


def test_run_without_the_block_is_not_reported(app, client, fake_llm):
    data = _generate_and_run(client, fake_llm, "print('just a mean')")
    assert data['methods'] == {'method': None, 'assumptions': [], 'effects': [],
                               'reported': False}
    ident = _identity(client)
    with app.app_context():
        meta = storage.load_run(data['run_id'], identity=ident)['meta']
    assert meta['methods'] == data['methods']


def test_legacy_run_with_null_methods_is_parsed_when_served(app, client):
    rid = _seed(app, client, BLOCK_OUTPUT)            # methods=None, as before SP3
    got = client.get(f'/runs/{rid}').get_json()
    assert got['methods']['reported'] is True
    assert got['methods']['method']['test'] == 'Welch t-test'
    ident = _identity(client)
    with app.app_context():                           # a read never writes
        assert storage.load_run(rid, identity=ident)['meta']['methods'] is None


def test_stored_card_is_normalized_when_served(app, client):
    bad = {'method': {'test': 'x' * 5000, 'n_used': True},
           'assumptions': [{'name': 'n', 'status': '<img src=x>'}] * 99,
           'effects': 'nope', 'reported': 'yes'}
    rid = _seed(app, client, 'out', methods=bad)
    got = client.get(f'/runs/{rid}').get_json()['methods']
    assert len(got['method']['test']) == methods.FIELD_MAX
    assert got['method']['n_used'] is None
    assert len(got['assumptions']) == methods.MAX_ASSUMPTIONS
    assert {a['status'] for a in got['assumptions']} == {'unknown'}
    assert got['effects'] == []


# ---------------------------------------------------------------------------
# /interpret and /generate_report get the card server-side
# ---------------------------------------------------------------------------

def test_interpret_grounds_on_the_runs_card(app, client, fake_llm):
    rid = _seed(app, client, BLOCK_OUTPUT, methods=methods.parse(BLOCK_OUTPUT))
    _seed(app, client, 'later run, no block')
    sse_events(post_json(client, '/interpret', {
        'run_id': rid,
        'methods': {'method': {'test': 'CLIENT_SPOOF'}},
        'methods_card': 'CLIENT_SPOOF'}))
    p = _prompt(fake_llm, 'interpret')
    assert _inside(p, 'Method: Welch t-test')
    assert _inside(p, 'Normality (Shapiro-Wilk): W=0.98, p=0.21 -> PASS')
    assert 'CLIENT_SPOOF' not in p


def test_interpret_says_assumptions_not_reported(app, client, fake_llm):
    _seed(app, client, 'Mean income 42000')
    sse_events(post_json(client, '/interpret', {}))
    p = _prompt(fake_llm, 'interpret')
    assert _inside(p, 'Assumptions not reported')


def test_interpret_of_a_legacy_run_parses_its_output(app, client, fake_llm):
    _seed(app, client, BLOCK_OUTPUT)                  # methods=None
    sse_events(post_json(client, '/interpret', {}))
    assert _inside(_prompt(fake_llm, 'interpret'), 'Method: Welch t-test')


def test_interpret_without_a_server_run_has_no_card(app, client, fake_llm):
    csrf_token(client)
    sse_events(post_json(client, '/interpret', {'output': BLOCK_OUTPUT,
                                                'success': True}))
    assert 'METHODS CARD' not in _prompt(fake_llm, 'interpret')


def test_failed_run_diagnosis_has_no_card(app, client, fake_llm):
    ident = _identity(client)
    with app.app_context():
        storage.save_run(BLOCK_OUTPUT + 'Traceback (most recent call last):\n',
                         [], script="print('x')", language='Python',
                         identity=ident, returncode=1)
    sse_events(post_json(client, '/interpret', {}))
    assert 'METHODS CARD' not in _prompt(fake_llm, 'interpret_debug')


def test_report_carries_each_selected_runs_card(app, client, fake_llm):
    r1 = _seed(app, client, BLOCK_OUTPUT, interp='I1',
               methods=methods.parse(BLOCK_OUTPUT), prompt='First question')
    _seed(app, client, 'OUT_TWO', interp='I2')
    r3 = _seed(app, client, 'no block here', interp='I3')
    events = sse_events(post_json(client, '/generate_report', {
        'run_ids': [r1, r3], 'methods': 'CLIENT_SPOOF'}))
    assert events[-1]['type'] == 'done'
    for kind in ('report_draft', 'report'):
        p = _prompt(fake_llm, kind)
        assert _inside(p, 'Method: Welch t-test'), kind
        assert _inside(p, f'=== Run {r1}: First question ===')
        assert _inside(p, f'=== Run {r3} ===')
        assert _inside(p, 'Assumptions not reported')
        assert 'CLIENT_SPOOF' not in p
        begin = p.index('<<<BEGIN METHODS CARD')
        assert (p.index('Method: Welch t-test', begin)
                < p.index('Assumptions not reported', begin))     # run order


def test_report_single_run_card_has_no_heading(app, client, fake_llm):
    _seed(app, client, BLOCK_OUTPUT, interp='I1',
          methods=methods.parse(BLOCK_OUTPUT))
    sse_events(post_json(client, '/generate_report', {}))
    p = _prompt(fake_llm, 'report_draft')
    assert _inside(p, 'Method: Welch t-test')
    assert '=== Run' not in p


def test_report_without_a_server_run_has_no_card(app, client, fake_llm):
    csrf_token(client)
    sse_events(post_json(client, '/generate_report', {
        'output': 'client output', 'interpretation': 'client interp'}))
    assert 'METHODS CARD' not in _prompt(fake_llm, 'report_draft')


def test_report_cards_are_clamped(app, client, fake_llm):
    from statlee.routes.report import REPORT_METHODS_MAX
    huge = {'method': {'test': 'T' * 300},
            'assumptions': [{'name': f'a{i}' + 'n' * 200, 'detail': 'd' * 400,
                             'status': 'pass'} for i in range(40)],
            'effects': [{'text': 'e' * 400} for _ in range(20)]}
    ids = [_seed(app, client, f'out {i}', interp='i', methods=huge)
           for i in range(5)]
    sse_events(post_json(client, '/generate_report', {'run_ids': ids}))
    p = _prompt(fake_llm, 'report_draft')
    begin = p.index('<<<BEGIN METHODS CARD')
    end = p.index('<<<END METHODS CARD>>>')
    assert end - begin <= REPORT_METHODS_MAX + 200
