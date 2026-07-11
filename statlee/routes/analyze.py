"""Analysis pipeline routes: code generation, guarded execution,
interpretation/auto-debugging, and the method picker.

Implements roadmap items 0.4 (run-guard), 5.5 (streamed draft), 5.11
(auto-debug), 5.12 (refinement mode), 5.14 (method picker), 5.2 (multi-plot),
plus the per-analysis usage accounting from 3.4.
"""
import json
import logging
import os

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from .. import billing, datatools, llm, prompts, sandbox, storage
from ..extensions import limiter
from ..identity import current_user_or_none
from ..usage import usage_breakdown
from . import (
    FREE_TEXT_MAX,
    STYLE_FIELD_MAX,
    clamp,
    clamp_codebook,
    clamp_history,
    clamp_plots,
    json_error,
    moderation_blocked,
    sse_event,
    sse_stream,
    strip_code_fences,
)

logger = logging.getLogger('statlee.analyze')

bp = Blueprint('analyze', __name__)


def _cfg():
    return current_app.config['STATLEE']


def _sum_usage(*usages):
    # Delegate to the shared aggregator so every client payload also carries a
    # per-model breakdown for the session-cost display (3.4).
    return usage_breakdown(*usages)


# ---------------------------------------------------------------------------
# /chat — moderation → feature selection → streamed draft → streamed validation
# ---------------------------------------------------------------------------

@bp.route('/chat', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def chat():
    data = request.get_json(silent=True) or {}
    # P1-7: clamp everything that flows into prompts before any LLM call --
    # unbounded client text here is unmetered input-token spend on the
    # operator's key.
    user_prompt = clamp(data.get('prompt'), FREE_TEXT_MAX)
    filename = data.get('filename')
    # 'language' is an enum-ish UI value but still client text interpolated
    # into the draft and validation prompts; clamp (and stringify) it so a
    # non-string payload cannot raise inside prompt construction.
    target_language = clamp(data.get('language'), STYLE_FIELD_MAX) or 'Python'
    history = clamp_history(data.get('history', []))
    codebook = clamp_codebook(data.get('codebook', {}) or {})
    current_code = clamp(data.get('current_code'), FREE_TEXT_MAX).strip() or None  # 5.12
    pro_mode = bool(data.get('pro'))  # "Pro mode": code-gen on the bigger model

    if not user_prompt or not filename:
        return json_error('Missing prompt or filename')

    service = llm.get_service()

    # Gate 1: moderation (synchronous — must 403 before any stream starts).
    # Structured verdict + default-deny: a malformed/ambiguous reply blocks.
    try:
        mod = service.generate('lite', prompts.moderation(user_prompt),
                               temperature=0.0, json_mode=True)
    except Exception:
        logger.exception("Moderation service failed")
        return json_error('Moderation service failed.', 503)
    blocked, reason = moderation_blocked(mod.text)
    if blocked:
        return json_error(f'Request denied. {reason}', 403)

    # Gate 2: the dataset must resolve and read BEFORE any money moves. An
    # invalid/expired filename (anonymous TTL cleanup makes stale tabs common)
    # or an unreadable CSV returns here without touching credits or the
    # operator's monthly priority ceiling (P1-1).
    filepath = storage.active_dataset_path(filename)
    if not filepath or not os.path.exists(filepath):
        return json_error('Invalid filename')
    try:
        df = pd.read_csv(filepath, nrows=100)
        headers = df.columns.tolist()
        metadata_summary = datatools.metadata_json(df)
    except Exception:
        logger.exception("Could not read dataset for /chat")
        return json_error('Could not read dataset headers.', 500)

    # Monetization seam (E): one chokepoint decides whether the costlier Pro-mode
    # request is allowed for this user. No-op unless billing is enabled. Runs
    # AFTER moderation and dataset validation (moderate -> validate -> debit)
    # so a blocked or doomed request never costs a credit or a unit of the
    # operator's monthly priority ceiling.
    billing_user = current_user_or_none()
    allowed, deny_msg = billing.check_and_debit(
        billing_user, priority=pro_mode, config=_cfg())
    if not allowed:
        return json_error(deny_msg or 'Out of credits.', 402)

    # Stage 1: feature selection on wide datasets.
    cfg = _cfg()
    filtered_headers = headers
    filtered_codebook = codebook
    selection_usage = None
    selection_degraded = False   # feature selection failed -> full-schema fallback
    if len(headers) >= cfg.feature_selection_threshold:
        column_context = (json.dumps(codebook, indent=2)
                          if codebook else ', '.join(headers))
        try:
            sel = service.generate(
                'flash',
                prompts.feature_selection(column_context, user_prompt),
                temperature=0.1, json_mode=True)
            selection_usage = sel.usage
            candidates = json.loads(sel.text).get('required_columns', [])
            if isinstance(candidates, list):
                validated = [c for c in candidates if c in headers]
                if validated:
                    filtered_headers = validated
                    if codebook:
                        filtered_codebook = {
                            c: codebook[c] for c in validated if c in codebook}
            logger.info("[/chat] Stage 1 selected %d/%d columns",
                        len(filtered_headers), len(headers))
        except Exception:
            logger.warning("[/chat] feature selection failed; using full schema",
                           exc_info=True)
            selection_degraded = True
    else:
        logger.info("[/chat] Stage 1 skipped: %d columns below threshold (%d)",
                    len(headers), cfg.feature_selection_threshold)

    def generate():
        draft_usage, validation_usage = {}, {}
        try:
            # Built inside the try (it cannot move above check_and_debit: it
            # depends on the paid feature-selection pass, which must stay
            # behind the debit) so a prompt-construction failure lands on the
            # refund path below instead of stranding the debited credit.
            draft_prompt = prompts.draft(
                filename, filtered_headers, filtered_codebook, target_language,
                metadata_summary, history, user_prompt,
                current_code=current_code)
            # Surface the feature-selection fallback (P2-8): the user already
            # paid for the request, so if Stage 1 failed and we quietly reverted
            # to the full schema, tell them rather than swallowing it server-side.
            if selection_degraded:
                yield sse_event({
                    'type': 'phase', 'phase': 'feature_selection_skipped',
                    'message': ('Column pre-selection was unavailable; analyzing '
                                'with the full dataset schema.')})
            # Phase A: stream the draft (5.5) — the slowest step is now visible.
            # Pro mode routes code generation to the bigger 'pro_max' model.
            yield sse_event({'type': 'phase', 'phase': 'drafting'})
            draft_accumulated = ''
            for delta in service.stream('pro_max' if pro_mode else 'draft',
                                        draft_prompt, temperature=0.1,
                                        usage_out=draft_usage):
                draft_accumulated += delta
                yield sse_event({'type': 'delta', 'text': delta})
            draft_code = strip_code_fences(draft_accumulated)

            # Phase B: stream the validation pass (replaces the draft client-side).
            yield sse_event({'type': 'phase', 'phase': 'validating'})
            validated = ''
            for delta in service.stream(
                    'lite',
                    prompts.validation(target_language, filename, draft_code),
                    temperature=0.0,
                    usage_out=validation_usage):
                validated += delta
                yield sse_event({'type': 'delta', 'text': delta})

            final_code = strip_code_fences(validated)
            # 0.4 run-guard: the server remembers what it produced; /run only
            # executes this (or a re-moderated edit of it).
            storage.save_approved_script(final_code, target_language)

            usage = _sum_usage(mod.usage, selection_usage,
                               draft_usage, validation_usage)
            yield sse_event({'type': 'done', 'code': final_code,
                             'language': target_language, 'usage': usage})
        except Exception:
            logger.exception("Code generation stream failed")
            # The credit (if any) was debited before streaming began; the stream
            # produced no usable script, so refund it rather than charge for
            # nothing (P1-3). Best-effort: a refund failure must not mask the
            # original error the user needs to see.
            try:
                billing.refund(billing_user, priority=pro_mode, config=cfg)
            except Exception:
                logger.exception("Failed to refund credit after stream failure")
            yield sse_event({'type': 'error',
                             'message': 'Code generation failed. Please try again.'})

    return sse_stream(generate)


# ---------------------------------------------------------------------------
# /run — guarded local execution (0.4 + Tier 0 sandbox + 5.2 multi-plot)
# ---------------------------------------------------------------------------

@bp.route('/run', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_run)
def run_code():
    data = request.get_json(silent=True) or {}
    code = data.get('code')
    language = data.get('language') or 'Python'
    filename = data.get('filename')

    if not code:
        return json_error('No code provided.')

    approved = storage.load_approved_script()
    if not approved:
        return json_error(
            'No generated script found for this session. Generate code '
            'before running.', 403)

    if code != approved.get('code'):
        # User-edited script (5.4): re-moderate before execution (0.4b).
        try:
            verdict = llm.get_service().generate(
                'lite', prompts.code_moderation(code, language),
                temperature=0.0, json_mode=True)
        except Exception:
            logger.exception("Run-guard moderation failed")
            return json_error(
                'Could not verify the edited script. Please try again.', 503)
        blocked, reason = moderation_blocked(verdict.text)
        if blocked:
            return json_error(
                f'Edited script rejected by the safety check. {reason}', 403)
        storage.save_approved_script(code, language)

    dataset_path = None
    dataset_name = None
    if filename:
        dataset_path = storage.active_dataset_path(filename)
        dataset_name = os.path.basename(storage.resolve_path(filename) or '') or None

    cfg = _cfg()
    result = sandbox.run_in_sandbox(
        code, language, dataset_path=dataset_path, dataset_name=dataset_name,
        timeout=cfg.exec_timeout, memory_mb=cfg.exec_memory_mb,
        output_limit=cfg.exec_output_limit, mode=cfg.sandbox_mode,
        runner_image=cfg.runner_image)

    storage.save_last_run(result.output, result.plots)

    if result.timed_out:
        return json_error(result.output, 500)

    return jsonify({
        'output': result.output,
        'plots': result.plots,
        'plot': result.plots[0] if result.plots else None,  # back-compat
        'success': result.success,
        'returncode': result.returncode,
    })


# ---------------------------------------------------------------------------
# /interpret — single-pass streamed interpretation + auto-debugging (5.11)
# ---------------------------------------------------------------------------

@bp.route('/interpret', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def interpret_results():
    data = request.get_json(silent=True) or {}
    # P1-7: cap the client-supplied text and plot payloads before they reach
    # the prompt (this route has no moderation gate, so it is otherwise a
    # general-purpose LLM proxy on the operator's key).
    final_output = clamp(data.get('output'), FREE_TEXT_MAX)
    plots = data.get('plots')
    if plots is None:
        plots = [data.get('plot')] if data.get('plot') else []
    plots = clamp_plots(plots)
    code = clamp(data.get('code'), FREE_TEXT_MAX)
    if 'success' in data:
        failed = not data.get('success')
    else:
        failed = bool(
            'Traceback (most recent call last)' in final_output
            or final_output.strip().startswith('Error'))

    if not final_output.strip() and not plots:
        return jsonify({'interpretation':
                        'No statistical output or plots generated to interpret.'})

    prompt = prompts.interpret(final_output, bool(plots), failed=failed,
                               code=code if failed else None)

    contents = [prompt]
    if plots and not failed:
        import base64
        for b64 in plots[:3]:
            try:
                contents.append(llm.MediaPart(
                    data=base64.b64decode(b64), mime_type='image/png'))
            except Exception:
                logger.warning("Skipping undecodable plot for interpretation")

    service = llm.get_service()

    def generate():
        usage = {}
        try:
            for delta in service.stream('flash', contents, temperature=0.3,
                                        usage_out=usage):
                yield sse_event({'type': 'delta', 'text': delta})
            yield sse_event({'type': 'done', 'debug': failed,
                             'usage': _sum_usage(usage)})
        except Exception:
            logger.exception("Interpretation stream failed")
            yield sse_event({'type': 'error',
                             'message': 'Failed to generate AI interpretation.'})

    return sse_stream(generate)


# ---------------------------------------------------------------------------
# /method_prompt — analysis catalog → tailored prompt (5.14)
# ---------------------------------------------------------------------------

@bp.route('/method_prompt', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def method_prompt():
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    method = data.get('method') or {}
    codebook = data.get('codebook', {}) or {}
    pdf_mapping = data.get('pdf_mapping', {}) or {}
    if not filename or not method.get('name'):
        return json_error('Missing filename or method')

    filepath = storage.active_dataset_path(filename)
    if not filepath or not os.path.exists(filepath):
        return json_error('Invalid filename')
    try:
        df = pd.read_csv(filepath, nrows=100)
        context = datatools.build_column_context(df, codebook, pdf_mapping)
        result = llm.get_service().generate(
            'flash',
            prompts.method_prompt(method['name'],
                                  method.get('description', ''), context),
            temperature=0.4, json_mode=True)
        payload = json.loads(result.text)
        return jsonify({'status': 'success',
                        'prompt': payload.get('prompt', ''),
                        'rationale': payload.get('rationale', ''),
                        'usage': usage_breakdown(result.usage)})
    except Exception:
        logger.exception("method_prompt failed")
        return json_error('Could not draft a prompt for that method.', 500)
