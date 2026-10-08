"""SP3: the lenient parser for the methods and assumptions block a generated
script prints at the end of its output (``statlee/methods.py``)."""
import json

import pytest

from statlee import methods

PYTHON_OUT = """\
Group means:
group
A    29.0
B    35.5
Levene: W=0.42, p=0.55
== METHOD ==
test: Welch t-test; outcome: income; predictors: group; n_used: 120; dropped: 4 (missing income)
== ASSUMPTIONS ==
Normality (Shapiro-Wilk, per group): W=0.98, p=0.21 -> PASS
Equal variances (Levene): W=0.42, p=0.55 -> PASS
Outliers: 3 points beyond 3 SD -> WARN
Independence: repeated respondents found -> FAIL
== EFFECT ==
Cohen's d = 0.41 [95% CI 0.12, 0.70]
Mean difference = 5.2 [95% CI 1.1, 9.3]
== END ==
"""

R_OUT = """\
Call:
lm(formula = income ~ age, data = d)
[1] "== METHOD =="
[1] "test: OLS regression; outcome: income; predictors: age, group; n_used: 98; dropped: 2 (missing age)"
[1] "== ASSUMPTIONS =="
[1] "Normality of residuals: Shapiro p = 0.03 -> WARN"
[1] "Homoscedasticity (Breusch-Pagan): p = 0.40 -> PASS"
[1] "== EFFECT =="
[1] "R-squared = 0.31 [95% CI 0.18, 0.44]"
[1] "== END =="
"""


def test_python_block_is_parsed():
    card = methods.parse(PYTHON_OUT)
    assert card['reported'] is True
    assert card['method'] == {
        'test': 'Welch t-test', 'outcome': 'income', 'predictors': 'group',
        'n_used': 120, 'dropped': 4, 'dropped_reason': 'missing income'}
    assert card['assumptions'] == [
        {'name': 'Normality (Shapiro-Wilk, per group)', 'detail': 'W=0.98, p=0.21',
         'status': 'pass'},
        {'name': 'Equal variances (Levene)', 'detail': 'W=0.42, p=0.55', 'status': 'pass'},
        {'name': 'Outliers', 'detail': '3 points beyond 3 SD', 'status': 'warn'},
        {'name': 'Independence', 'detail': 'repeated respondents found', 'status': 'fail'},
    ]
    assert card['effects'] == [{'text': "Cohen's d = 0.41 [95% CI 0.12, 0.70]"},
                               {'text': 'Mean difference = 5.2 [95% CI 1.1, 9.3]'}]


def test_r_print_prefixes_and_quotes_are_stripped():
    card = methods.parse(R_OUT)
    assert card['reported'] is True
    assert card['method']['test'] == 'OLS regression'
    assert card['method']['predictors'] == 'age, group'
    assert card['method']['n_used'] == 98
    assert card['method']['dropped'] == 2
    assert card['method']['dropped_reason'] == 'missing age'
    assert [a['status'] for a in card['assumptions']] == ['warn', 'pass']
    assert card['assumptions'][0]['name'] == 'Normality of residuals'
    assert card['effects'] == [{'text': 'R-squared = 0.31 [95% CI 0.18, 0.44]'}]


def test_r_cat_output_without_prefix_and_escaped_quotes():
    out = ('== METHOD ==\n'
           'test: chi-square; outcome: vote; predictors: region; n_used: 50; dropped: 0\n'
           '== ASSUMPTIONS ==\n'
           '[1] "Expected counts: min 6.1 \\"ok\\" -> PASS"\n'
           '== END ==\n')
    card = methods.parse(out)
    assert card['method']['dropped'] == 0
    assert card['method']['dropped_reason'] is None
    assert card['assumptions'] == [
        {'name': 'Expected counts', 'detail': 'min 6.1 "ok"', 'status': 'pass'}]


def test_case_spacing_and_windows_line_endings():
    out = ('noise\r\n'
           '  ==  method  ==  \r\n'
           'Test :  t-test ;OUTCOME: y;  N_Used : 1,234 ; Dropped: 3\r\n'
           '=== Assumptions ===\r\n'
           'normality :  p=0.5  ->  pass\r\n'
           'variance: p=0.01 => Fail.\r\n'
           'linearity: looks fine -> warning\r\n'
           '==effect==\r\n'
           '  d = 0.2  \r\n'
           '==end==\r\n')
    card = methods.parse(out)
    assert card['method']['test'] == 't-test'
    assert card['method']['outcome'] == 'y'
    assert card['method']['n_used'] == 1234
    assert card['method']['dropped'] == 3
    assert [(a['name'], a['detail'], a['status']) for a in card['assumptions']] == [
        ('normality', 'p=0.5', 'pass'), ('variance', 'p=0.01', 'fail'),
        ('linearity', 'looks fine', 'warn')]
    assert card['effects'] == [{'text': 'd = 0.2'}]


def test_missing_block_is_not_reported():
    card = methods.parse('Mean income: 42000\nSD: 1200\n')
    assert card == {'method': None, 'assumptions': [], 'effects': [], 'reported': False}


@pytest.mark.parametrize('value', [None, '', 42, b'== METHOD ==', ['x'], {'a': 1}])
def test_non_text_or_empty_input_never_raises(value):
    card = methods.parse(value)
    assert card['reported'] is False
    assert card['method'] is None


def test_missing_sections_are_tolerated():
    out = '== ASSUMPTIONS ==\nNormality: p=0.3 -> PASS\n== END ==\n'
    card = methods.parse(out)
    assert card['reported'] is True
    assert card['method'] is None
    assert card['effects'] == []
    assert card['assumptions'][0]['status'] == 'pass'

    only_method = '== METHOD ==\ntest: correlation; outcome: y; predictors: x\n'
    card = methods.parse(only_method)               # no END: runs to end of output
    assert card['reported'] is True
    assert card['method']['test'] == 'correlation'
    assert card['method']['n_used'] is None
    assert card['assumptions'] == []


def test_method_keys_on_separate_lines_and_extra_lines():
    out = ('== METHOD ==\n'
           'test: ANOVA\n'
           'outcome: score\n'
           'some stray text with no key\n'
           'n_used: n/a\n'
           '== ASSUMPTIONS ==\n'
           '\n'
           '-----\n'
           'Homogeneity: Levene p = 0.2 -> PASS\n'
           'A free-form note without a verdict\n'
           '== END ==\n'
           'trailing output after the block\n')
    card = methods.parse(out)
    assert card['method']['test'] == 'ANOVA'
    assert card['method']['outcome'] == 'score'
    assert card['method']['n_used'] is None          # not a number: dropped
    names = [a['name'] for a in card['assumptions']]
    assert names == ['Homogeneity', 'A free-form note without a verdict']
    assert card['assumptions'][1]['status'] == 'unknown'


def test_repeated_block_takes_the_last():
    first = ('== METHOD ==\ntest: first; n_used: 10\n== ASSUMPTIONS ==\n'
             'a: x -> FAIL\n== END ==\n')
    second = ('== METHOD ==\ntest: second; n_used: 20\n== ASSUMPTIONS ==\n'
              'b: y -> PASS\n== END ==\n')
    card = methods.parse(first + 'between\n' + second)
    assert card['method']['test'] == 'second'
    assert card['method']['n_used'] == 20
    assert [a['name'] for a in card['assumptions']] == ['b']


def test_header_mentions_inside_ordinary_lines_are_not_headers():
    out = 'The METHOD == simple and ASSUMPTIONS hold\nEFFECT = big\n'
    assert methods.parse(out)['reported'] is False


def test_sizes_are_clamped():
    many = '\n'.join(f'check {i}: p=0.{i % 10} -> PASS' for i in range(500))
    effects = '\n'.join(f'effect {i} = {i}' for i in range(500))
    long_name = 'x' * 5000
    out = ('== METHOD ==\n'
           f'test: {long_name}; outcome: {long_name}\n'
           '== ASSUMPTIONS ==\n' + many + '\n'
           f'{long_name}: {long_name} -> WARN\n'
           '== EFFECT ==\n' + effects + '\n== END ==\n')
    card = methods.parse(out)
    assert len(card['assumptions']) == methods.MAX_ASSUMPTIONS
    assert len(card['effects']) == methods.MAX_EFFECTS
    assert len(card['method']['test']) <= methods.FIELD_MAX
    assert len(card['method']['outcome']) <= methods.FIELD_MAX
    for a in card['assumptions']:
        assert len(a['name']) <= methods.NAME_MAX
        assert len(a['detail']) <= methods.DETAIL_MAX
    for e in card['effects']:
        assert len(e['text']) <= methods.EFFECT_MAX
    assert len(json.dumps(card)) < 20000


def test_huge_output_with_block_at_the_end_is_found():
    out = ('filler line\n' * 50000) + PYTHON_OUT
    card = methods.parse(out)
    assert card['reported'] is True
    assert card['method']['n_used'] == 120


def test_control_characters_are_removed():
    out = ('== METHOD ==\ntest: t\x00-test\x1b[31m; outcome: y\n'
           '== ASSUMPTIONS ==\nnorm\x07: ok -> PASS\n== END ==\n')
    card = methods.parse(out)
    assert '\x00' not in card['method']['test']
    assert '\x1b' not in card['method']['test']
    assert '\x07' not in card['assumptions'][0]['name']


def test_parse_never_raises_on_pathological_input():
    for text in ('== METHOD ==\n' * 1000, '== END ==\n== END ==',
                 '== ASSUMPTIONS ==\n: -> PASS\n->PASS\n', '[1] "\n[1] ""\n',
                 '\ud800 == METHOD ==\ntest: \ud800\n'):
        methods.parse(text)


# ---------------------------------------------------------------------------
# normalize / for_run / render_text
# ---------------------------------------------------------------------------

def test_normalize_accepts_only_the_card_shape():
    good = methods.parse(PYTHON_OUT)
    assert methods.normalize(good) == good
    assert methods.normalize(None) is None
    assert methods.normalize('x') is None
    bad = {'method': 'x', 'assumptions': [{'name': 1, 'status': 'evil'}, 'x'],
           'effects': [{'text': 'y' * 9999}, 3], 'reported': 'yes'}
    norm = methods.normalize(bad)
    assert norm['method'] is None
    assert norm['assumptions'] == [{'name': '1', 'detail': '', 'status': 'unknown'}]
    assert norm['effects'] == [{'text': 'y' * methods.EFFECT_MAX}]
    assert norm['reported'] is True


def test_for_run_parses_legacy_null_lazily():
    assert methods.for_run({'methods': None}, PYTHON_OUT)['method']['n_used'] == 120
    assert methods.for_run({}, 'no block')['reported'] is False
    stored = methods.parse(R_OUT)
    # A stored card wins over the output.
    assert methods.for_run({'methods': stored}, PYTHON_OUT) == stored


def test_render_text_for_the_prompt():
    text = methods.render_text(methods.parse(PYTHON_OUT))
    assert 'Welch t-test' in text
    assert 'N used: 120' in text
    assert 'Dropped: 4 (missing income)' in text
    assert 'Normality (Shapiro-Wilk, per group): W=0.98, p=0.21 -> PASS' in text
    assert 'Independence: repeated respondents found -> FAIL' in text
    assert "Cohen's d = 0.41 [95% CI 0.12, 0.70]" in text
    missing = methods.render_text(methods.parse('nothing'))
    assert 'not reported' in missing.lower()
    assert methods.render_text(None) == ''


def test_pathological_long_lines_stay_fast():
    import time
    spaces = ' ' * 300000
    out = ('== METHOD ==\ntest: ' + spaces + 'x\n== ASSUMPTIONS ==\n'
           'a' + spaces + 'b\n' + '- ' * 100000 + 'PASS\n'
           '== EFFECT ==\n' + '=' * 300000 + '\n== END ==\n')
    t0 = time.perf_counter()
    card = methods.parse(out)
    assert time.perf_counter() - t0 < 2
    assert card['reported'] is True


def test_unicode_arrow_and_bracketed_verdicts():
    out = ('== ASSUMPTIONS ==\n'
           'norm: p=0.1 → FAIL\n'
           'vif: max 2.1 [PASS]\n'
           'cells: min 4 (warning)\n'
           '== END ==\n')
    assert [a['status'] for a in methods.parse(out)['assumptions']] == ['fail', 'pass', 'warn']


# ---------------------------------------------------------------------------
# Fix round 1: negations, trailing notes, unfilled template lines
# ---------------------------------------------------------------------------

def _checks(*lines):
    out = '== ASSUMPTIONS ==\n' + '\n'.join(lines) + '\n== END ==\n'
    return [(a['name'], a['detail'], a['status'])
            for a in methods.parse(out)['assumptions']]


@pytest.mark.parametrize('line', [
    'Normality: did not pass',
    'Expected counts: not ok',
    'Equal variances: does not pass',
    "Linearity: doesn't pass",
    'Homogeneity: no pass',
    'Sphericity: never passed',
    'Independence: failed to pass',
    'Normality: fails to pass',
    'Normality: p=0.01 -> not PASS',
    'Normality: cannot pass',
    'Normality: can not pass',
    "Normality: can't pass",
    'Normality: can’t pass',
    'Normality: unable to pass',
    'Normality: no, pass',
    'Normality: not a pass',
    'Normality: not an ok',
    'Normality: never; pass',
])
def test_a_negated_verdict_is_never_a_pass(line):
    (_name, _detail, status), = _checks(line)
    assert status == 'unknown'


def test_plain_word_verdicts_without_negation_still_count():
    assert _checks('Normality: p=0.3 pass', 'VIF: max 2.1 OK.',
                   'Normality: cannot reject -> PASS', 'Fit: a pass',
                   'Outliers: none, pass') == [
        ('Normality', 'p=0.3', 'pass'), ('VIF', 'max 2.1', 'pass'),
        ('Normality', 'cannot reject', 'pass'), ('Fit', 'a', 'pass'),
        ('Outliers', 'none,', 'pass')]


def test_arrow_verdict_with_a_short_trailing_note_keeps_the_note():
    assert _checks(
        'Equal variances (Levene): p=0.01 -> FAIL (use Welch)',
        'Normality: p=0.02 -> FAIL; switched to Mann-Whitney',
        'Outliers: 2 beyond 3 SD -> WARN - kept them',
        'Cells: min 6 => PASS, all above 5',
    ) == [
        ('Equal variances (Levene)', 'p=0.01; use Welch', 'fail'),
        ('Normality', 'p=0.02; switched to Mann-Whitney', 'fail'),
        ('Outliers', '2 beyond 3 SD; kept them', 'warn'),
        ('Cells', 'min 6; all above 5', 'pass'),
    ]


def test_a_note_without_a_detail_becomes_the_detail():
    assert _checks('Linearity -> PASS (residual plot)') == [
        ('Linearity', 'residual plot', 'pass')]


def test_a_long_tail_after_an_arrow_is_not_a_verdict():
    line = 'Check: x -> PASS ' + 'y' * 400
    (_name, _detail, status), = _checks(line)
    assert status == 'unknown'


def test_unfilled_template_lines_are_skipped():
    from statlee import prompts
    card = methods.parse(prompts.METHODS_BLOCK)
    assert card == methods.empty()
    out = ('== METHOD ==\n'
           'test: t-test; outcome: <var>; predictors: <vars>; n_used: <int>; '
           'dropped: <int> (<reason>)\n'
           '== ASSUMPTIONS ==\n'
           '<check name>: <statistic/result> -> PASS|WARN|FAIL\n'
           'Normality: p=0.4 -> PASS|WARN|FAIL\n'
           'Levene: p=0.3 -> PASS\n'
           '== EFFECT ==\n'
           '<effect size name> = <value> [95% CI <lo>, <hi>]\n'
           'd = 0.3 [95% CI 0.1, 0.5]\n'
           '== END ==\n')
    card = methods.parse(out)
    assert card['method'] == {'test': 't-test', 'outcome': None, 'predictors': None,
                              'n_used': None, 'dropped': None, 'dropped_reason': None}
    assert card['assumptions'] == [{'name': 'Levene', 'detail': 'p=0.3', 'status': 'pass'}]
    assert card['effects'] == [{'text': 'd = 0.3 [95% CI 0.1, 0.5]'}]


def test_markup_like_values_are_not_mistaken_for_placeholders():
    out = ('== METHOD ==\ntest: <b>t</b>\n== ASSUMPTIONS ==\n'
           'n <5 cells: 0 of 4 -> PASS\n== END ==\n')
    card = methods.parse(out)
    assert card['method']['test'] == '<b>t</b>'
    assert card['assumptions'][0]['status'] == 'pass'
