"""Browser checks for the SP3 methods and assumptions card (2026-10-08).

Same harness as ``test_e2e_ui.py`` (live app on a free localhost port, every
model call answered by the deterministic ``FakeLLMService``, Chromium through
Playwright): $0, no real provider. The fake hands back a script; the real
sandbox runs it, so the card is parsed from genuine script output.

Set ``STATLEE_SHOTS_DIR`` to also write review screenshots of the card
(light/dark, 1440px/390px). Skips cleanly without Playwright.
"""
import os

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from test_e2e_ui import (  # noqa: E402,F401  (fixtures are used by name)
    CSV_A,
    _new_page,
    browser,
    e2e_server,
    fake,
    generate,
    page,
    run_script,
    skip_codebook,
    upload,
)

BLOCK_SCRIPT = (
    "print('Mean income by group: A=36000, B=47000')\n"
    "print('== METHOD ==')\n"
    "print('test: Welch t-test; outcome: income; predictors: group; "
    "n_used: 4; dropped: 1 (missing income)')\n"
    "print('== ASSUMPTIONS ==')\n"
    "print('Normality (Shapiro-Wilk): W=0.97, p=0.62 -> PASS')\n"
    "print('Equal variances (Levene): F=4.10, p=0.048 -> WARN')\n"
    "print('Independence: repeated respondent ids -> FAIL')\n"
    "print('== EFFECT ==')\n"
    "print(\"Cohen's d = 0.41 [95% CI 0.12, 0.70]\")\n"
    "print('== END ==')\n"
)
PLAIN_SCRIPT = "print('Mean income: 42000')\n"


def _script(fake, code):
    fake.set('draft', code)
    fake.set('validation', code)


def _run(page, fake, code, question):
    _script(fake, code)
    generate(page, question)
    run_script(page)


def _start(page, name):
    upload(page, name, CSV_A + '61,1,A\n')
    skip_codebook(page)


def _chips(page):
    return page.evaluate("""() => Array.from(
        document.querySelectorAll('#methodsCard .mc-chip')).map(c => [c.className, c.textContent])""")


def row(run_id):
    return f'#runList > li[data-run-id="{run_id}"]'


# ---------------------------------------------------------------------------
# The plan checkpoint: the card shows chips, and the fallback when missing
# ---------------------------------------------------------------------------

def test_card_shows_method_chips_and_effects(page, fake):
    _start(page, 'mc-block.csv')
    _run(page, fake, BLOCK_SCRIPT, 'Compare income by group')
    page.wait_for_selector('#methodsCard:not(.hidden)')
    card = page.inner_text('#methodsCard')
    assert 'Methods and assumptions'.lower() in card.lower()
    for text in ('Welch t-test', 'income', 'group', '4', '1 (missing income)',
                 'Normality (Shapiro-Wilk)', 'W=0.97, p=0.62',
                 "Cohen's d = 0.41 [95% CI 0.12, 0.70]"):
        assert text in card, text
    assert _chips(page) == [['mc-chip pass', 'PASS'], ['mc-chip warn', 'WARN'],
                            ['mc-chip fail', 'FAIL']]
    assert page.inner_text('#methodsCardSummary') == '1 pass · 1 warn · 1 fail'
    # The card sits above the terminal, and the raw block stays in it.
    above = page.evaluate("""() => {
        const c = document.getElementById('methodsCard').getBoundingClientRect();
        const t = document.getElementById('runOutput').getBoundingClientRect();
        return c.bottom <= t.top;
    }""")
    assert above
    assert '== METHOD ==' in page.inner_text('#runOutput')


def test_card_reads_assumptions_not_reported_without_the_block(page, fake):
    _start(page, 'mc-plain.csv')
    _run(page, fake, PLAIN_SCRIPT, 'Mean income')
    page.wait_for_selector('#methodsCard:not(.hidden)')
    assert 'Assumptions not reported' in page.inner_text('#methodsCard')
    assert _chips(page) == []


def test_a_failed_run_without_the_block_hides_the_card(page, fake):
    _start(page, 'mc-fail.csv')
    _run(page, fake, BLOCK_SCRIPT, 'Compare income by group')
    page.wait_for_selector('#methodsCard:not(.hidden)')
    fake.set('interpret_debug', '### What went wrong\nA typo.')
    _run(page, fake, "raise ValueError('boom')\n", 'Break it')
    page.wait_for_function(
        "() => document.getElementById('methodsCard').classList.contains('hidden')")


# ---------------------------------------------------------------------------
# The card follows the run being shown: rail view and history restore
# ---------------------------------------------------------------------------

def test_viewing_an_older_run_shows_its_own_card(page, fake):
    _start(page, 'mc-view.csv')
    _run(page, fake, BLOCK_SCRIPT, 'Compare income by group')
    _run(page, fake, PLAIN_SCRIPT, 'Mean income')
    page.wait_for_function("() => document.querySelectorAll('#runList > li').length === 2")
    first, second = page.evaluate(
        "() => Array.from(document.querySelectorAll('#runList > li')).map(li => li.dataset.runId)")
    assert 'Assumptions not reported' in page.inner_text('#methodsCard')

    page.click(row(first) + ' .wb-run-open')
    page.wait_for_selector('#runViewBar:not(.hidden)')
    page.wait_for_function(
        "() => document.getElementById('methodsCard').innerText.includes('Welch t-test')")
    assert len(_chips(page)) == 3

    page.click('#runBackBtn')
    page.wait_for_function(
        "() => document.getElementById('methodsCard').innerText.includes('Assumptions not reported')")
    assert second


def test_history_restore_brings_back_the_card(page, fake):
    _start(page, 'mc-hist.csv')
    _run(page, fake, BLOCK_SCRIPT, 'Compare income by group')
    page.wait_for_selector('#methodsCard:not(.hidden)')
    page.evaluate('() => CC.resetAnalysisState()')
    assert page.is_hidden('#methodsCard')

    page.click('#historyBtn')
    page.wait_for_selector('#historyList button')
    page.locator('#historyList button', has_text='Compare income by group').click()
    page.wait_for_function(
        "() => document.getElementById('methodsCard').innerText.includes('Welch t-test')")
    assert len(_chips(page)) == 3


def test_text_only_history_restore_shows_no_stale_card(page, fake):
    _start(page, 'mc-text.csv')
    _run(page, fake, BLOCK_SCRIPT, 'Compare income by group')
    page.wait_for_selector('#methodsCard:not(.hidden)')
    page.evaluate("""() => {
        const list = JSON.parse(localStorage.getItem('cc_history'));
        list.push({prompt: 'gone run', code: "print('gone')", language: 'Python',
                   dataset_name: list[0].dataset_name, output: 'text only output',
                   interpretation: 'old words', run_id: 'r0999', ts: Date.now() - 5000});
        localStorage.setItem('cc_history', JSON.stringify(list));
    }""")
    page.click('#historyBtn')
    page.wait_for_selector('#historyList button')
    page.locator('#historyList button', has_text='gone run').click()
    page.wait_for_function(
        "() => document.getElementById('runOutput').innerText.includes('text only output')")
    page.wait_for_timeout(300)
    assert page.is_hidden('#methodsCard')


# ---------------------------------------------------------------------------
# Safety and layout
# ---------------------------------------------------------------------------

def test_card_values_render_as_text_not_markup(page, fake):
    evil = (
        "print('== METHOD ==')\n"
        "print('test: <img src=x onerror=window.__pwned=1>; outcome: <b>y</b>')\n"
        "print('== ASSUMPTIONS ==')\n"
        "print('<script>window.__pwned=2</script>: <i>x</i> -> PASS')\n"
        "print('== EFFECT ==')\n"
        "print('<svg onload=window.__pwned=3>')\n"
        "print('== END ==')\n"
    )
    _start(page, 'mc-xss.csv')
    _run(page, fake, evil, 'Try markup')
    page.wait_for_selector('#methodsCard:not(.hidden)')
    assert page.evaluate(
        "() => document.querySelectorAll('#methodsCard img, #methodsCard script,"
        " #methodsCard svg, #methodsCard b, #methodsCard i').length") == 0
    assert '<img src=x' in page.inner_text('#methodsCard')
    assert page.evaluate('() => window.__pwned') is None


def test_long_output_stays_reachable_below_the_card(page, fake):
    # The card must not squeeze the terminal: long output scrolls inside it.
    long_script = BLOCK_SCRIPT + "for i in range(80):\n    print('row', i)\n"
    _start(page, 'mc-long.csv')
    _run(page, fake, long_script, 'Long output')
    page.wait_for_selector('#methodsCard:not(.hidden)')
    got = page.evaluate("""() => {
        const body = document.getElementById('runOutput').parentElement;
        body.scrollTop = body.scrollHeight;
        const style = getComputedStyle(body);
        return {height: body.getBoundingClientRect().height, overflow: style.overflowY,
                scrolled: body.scrollTop > 0};
    }""")
    assert got['height'] >= 150
    assert got['overflow'] == 'auto' and got['scrolled']


def test_card_fits_a_phone_screen(browser, e2e_server, fake):
    p = _new_page(browser, e2e_server['url'], width=390, height=844)
    try:
        _start(p, 'mc-narrow.csv')
        _run(p, fake, BLOCK_SCRIPT, 'Compare income by group')
        p.wait_for_selector('#methodsCard:not(.hidden)')
        width = p.evaluate('() => document.documentElement.scrollWidth')
        assert width <= 390, f'page is {width}px wide at 390px with the methods card'
    finally:
        p.context.close()


# ---------------------------------------------------------------------------
# Review screenshots (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get('STATLEE_SHOTS_DIR'),
                    reason='set STATLEE_SHOTS_DIR to write review screenshots')
@pytest.mark.parametrize('scheme', ['light', 'dark'])
@pytest.mark.parametrize('width,height', [(1440, 900), (390, 844)])
def test_methods_screenshots(browser, e2e_server, fake, scheme, width, height):
    out = os.environ['STATLEE_SHOTS_DIR']
    os.makedirs(out, exist_ok=True)
    p = _new_page(browser, e2e_server['url'], width=width, height=height, scheme=scheme)
    try:
        if scheme == 'dark':
            p.evaluate("() => { if (!document.documentElement.classList.contains('dark')) CC.toggleTheme(); }")
        _start(p, f'mc-shot-{scheme}-{width}.csv')
        tag = f'{scheme}-{width}'
        _run(p, fake, BLOCK_SCRIPT, 'Compare income by group')
        p.wait_for_selector('#methodsCard:not(.hidden)')
        p.evaluate("() => document.getElementById('methodsCard').scrollIntoView()")
        p.locator('#methodsCard').screenshot(path=os.path.join(out, f'methods-card-{tag}.png'))
        p.screenshot(path=os.path.join(out, f'methods-results-{tag}.png'), full_page=True)
        _run(p, fake, PLAIN_SCRIPT, 'Mean income')
        p.wait_for_function(
            "() => document.getElementById('methodsCard').innerText.includes('Assumptions not reported')")
        p.locator('#methodsCard').screenshot(path=os.path.join(out, f'methods-missing-{tag}.png'))
    finally:
        p.context.close()
