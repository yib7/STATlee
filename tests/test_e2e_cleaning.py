"""Browser checks for the data cleaning panel's version controls: Undo, Redo
and Original (revert) clicked as a user would, docked and popped out
(release audit 2026-10-16, 3A C-17 and checklist 3.13).

Same harness as ``test_e2e_ui.py`` (live app on a free localhost port, every
model call answered by the deterministic ``FakeLLMService``, Chromium through
Playwright): $0, no real provider. Skips cleanly without Playwright.
"""
import json

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from test_e2e_ui import (  # noqa: E402,F401  (fixtures are used by name)
    CSV_A,
    browser,
    e2e_server,
    fake,
    page,
    skip_codebook,
    upload,
)

ORIGINAL = ['age', 'income', 'group']
ADDED = ORIGINAL + ['age2']


def _add_column(fake):
    fake.set('wrangle', json.dumps({'code': "df['age2'] = df['age'] * 2",
                                    'summary': 'Added age2', 'error': None}))


def headers_are(page, cols):
    page.wait_for_function(
        f"() => JSON.stringify(CC.state.headers) === {json.dumps(json.dumps(cols, separators=(',', ':')))}")


def buttons_settle(page, undo, redo, revert):
    """Wait until Undo / Redo / Original read exactly these enabled states."""
    page.wait_for_function(
        "([u, r, v]) => !document.getElementById('undoBtn').disabled === u"
        " && !document.getElementById('redoBtn').disabled === r"
        " && !document.getElementById('revertBtn').disabled === v",
        arg=[undo, redo, revert])


def apply(page, instruction):
    page.fill('#wrangleInput', instruction)
    page.click('#wrangleApplyBtn')
    page.wait_for_selector('#changelogList .wrangle-turn:not(.pending)')


def test_undo_redo_revert_buttons_follow_can_undo_can_redo(page, fake):
    upload(page, 'clean-ur.csv', CSV_A + '91,1,A\n')
    skip_codebook(page)
    page.wait_for_selector('#changelogPanel:not(.hidden)')
    buttons_settle(page, undo=False, redo=False, revert=False)   # only v1

    _add_column(fake)
    apply(page, 'add age doubled')
    headers_are(page, ADDED)
    buttons_settle(page, undo=True, redo=False, revert=True)

    page.click('#undoBtn')
    headers_are(page, ORIGINAL)
    buttons_settle(page, undo=False, redo=True, revert=True)
    assert 'age2' not in page.text_content('#dataTableHead')

    page.click('#redoBtn')
    headers_are(page, ADDED)
    buttons_settle(page, undo=True, redo=False, revert=True)
    page.wait_for_function(
        "() => document.getElementById('dataTableHead').textContent.includes('age2')")

    # Original: back to the upload, as a new version that Undo can take back.
    page.click('#revertBtn')                       # the confirm is accepted
    headers_are(page, ORIGINAL)
    page.wait_for_selector('.cc-toast.success >> text=Reverted to the original upload')
    buttons_settle(page, undo=True, redo=False, revert=True)
    page.click('#undoBtn')
    headers_are(page, ADDED)


def test_popped_out_cleaning_keeps_undo_redo_and_original(page, fake):
    upload(page, 'clean-pop.csv', CSV_A + '92,1,A\n')
    skip_codebook(page)
    page.wait_for_selector('#changelogPanel:not(.hidden)')
    page.click('#wranglePopoutBtn')
    page.wait_for_selector('#wrangleFloat')
    assert not page.is_visible('#changelogPanel')
    # The version controls travel with the floating tool.
    for bid in ('undoBtn', 'redoBtn', 'revertBtn'):
        assert page.locator(f'#wrangleFloat #{bid}').count() == 1, bid
        assert page.is_visible(f'#{bid}'), bid

    _add_column(fake)
    apply(page, 'add age doubled')
    headers_are(page, ADDED)
    # An Apply in the float used to un-hide the sidebar panel as an empty
    # header whose Pop out did nothing.
    assert not page.is_visible('#changelogPanel')
    buttons_settle(page, undo=True, redo=False, revert=True)
    page.click('#wrangleFloat #undoBtn')
    headers_are(page, ORIGINAL)
    buttons_settle(page, undo=False, redo=True, revert=True)

    page.click('#wrangleDockBtn')
    page.wait_for_selector('#wrangleFloat', state='detached')
    page.wait_for_selector('#changelogPanel:not(.hidden)')
    for bid in ('undoBtn', 'redoBtn', 'revertBtn'):
        assert page.locator(f'#changelogPanel #{bid}').count() == 1, bid
    page.click('#redoBtn')
    headers_are(page, ADDED)
