"""Analysis pipeline routes: code generation, guarded execution,
interpretation/auto-debugging, and the method picker.

Implements roadmap items 0.4 (run-guard), 5.5 (streamed draft), 5.11
(auto-debug), 5.12 (refinement mode), 5.14 (method picker), 5.2 (multi-plot),
plus the per-analysis usage accounting from 3.4.
"""
import base64
import logging
import os
import re

from flask import Blueprint, current_app, jsonify

from .. import billing, codecheck, datatools, llm, prompts, sandbox, storage
from ..extensions import limiter
from ..identity import current_user_or_none
from ..usage import usage_breakdown
from . import (
    BACKGROUND_MAX,
    FREE_TEXT_MAX,
    STYLE_FIELD_MAX,
    DebitGuard,
    clamp,
    clamp_codebook,
    clamp_history,
    clamp_mapping,
    clamp_plots,
    dataset_filename_field,
    dict_field,
    json_error,
    json_object_reply,
    language_field,
    llm_error_payload,
    llm_json_error,
    moderation_blocked,
    moderation_gate,
    register_field_errors,
    request_json,
    sse_event,
    sse_stream,
    str_field,
    strip_code_fences,
)
from .datasets import read_active_frame

logger = logging.getLogger('statlee.analyze')

bp = Blueprint('analyze', __name__)
register_field_errors(bp)


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
    data = request_json()
    # P1-7: clamp everything that flows into prompts before any LLM call --
    # unbounded client text here is unmetered input-token spend on the
    # operator's key.
    user_prompt = clamp(data.get('prompt'), FREE_TEXT_MAX)
    filename = dataset_filename_field(data)
    # 'language' selects the interpreter, the static check and the approval
    # key, so it is validated as an enum (D1-12), not free text.
    target_language = language_field(data)
    history = clamp_history(data.get('history', []))
    codebook = clamp_codebook(data.get('codebook', {}) or {})
    current_code = clamp(data.get('current_code'), FREE_TEXT_MAX).strip() or None  # 5.12
    pro_mode = bool(data.get('pro'))  # "Pro mode": code-gen on the bigger model

    if not user_prompt or not filename:
        return json_error('Missing prompt or filename')

    service = llm.get_service()

    # Gate 1: moderation (synchronous — must 403 before any stream starts).
    # Structured verdict + default-deny: a malformed/ambiguous reply blocks.
    denied, mod = moderation_gate(service, prompts.moderation(user_prompt),
                                  log_message="Moderation service failed")
    if denied is not None:
        return denied

    # Gate 2: the dataset must resolve and read BEFORE any money moves. An
    # invalid/expired filename (anonymous TTL cleanup makes stale tabs common)
    # or an unreadable CSV returns here without touching credits or the
    # operator's monthly priority ceiling (P1-1).
    try:
        # D3-7: the full (cached) frame, so the draft's missing counts, dtypes
        # and ranges describe the whole dataset rather than its first 100 rows.
        df, _ = read_active_frame(filename)
    except Exception:
        logger.exception("Could not read dataset for /chat")
        return json_error('Could not read dataset headers.', 500)
    if df is None:
        return json_error('Invalid filename')
    headers = df.columns.tolist()

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
    debit = DebitGuard(billing_user, priority=pro_mode, config=cfg)
    filtered_headers = headers
    filtered_codebook = codebook
    selection_usage = None
    selection_degraded = False   # feature selection failed -> full-schema fallback
    if len(headers) >= cfg.feature_selection_threshold:
        # D1-5/D3-6: list EVERY column, annotated by its codebook entry where
        # one exists. Sending only the codebook (when present) hid every column
        # the user had not classified, so selection could never pick them.
        column_context = '\n'.join(
            f'- {h}: {clamp(codebook[h], 200)}' if h in codebook else f'- {h}'
            for h in map(str, headers))
        try:
            sel = service.generate(
                'flash',
                prompts.feature_selection(column_context, user_prompt),
                temperature=0.1, json_mode=True)
            selection_usage = sel.usage
            candidates = json_object_reply(sel.text).get('required_columns')
            if not isinstance(candidates, list):
                raise llm.MalformedReplyError(detail='required_columns')
            if candidates:
                header_set = set(headers)
                validated = list(dict.fromkeys(
                    c for c in candidates
                    if isinstance(c, str) and c in header_set))
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
            # D1-5/D3-6: the metadata covers the columns Stage 1 picked (all of
            # them when it was skipped), not whatever the first 80 happened to be.
            metadata_summary = datatools.metadata_json(
                df[filtered_headers],
                max_columns=min(max(80, len(filtered_headers)), 300))
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
            if not draft_code.strip():
                # D3-4: nothing usable to validate (e.g. a reply that was
                # only a code fence).
                raise llm.EmptyGenerationError()

            # Phase B: stream the validation pass (replaces the draft client-side).
            yield sse_event({'type': 'phase', 'phase': 'validating'})
            validated = ''
            # D3-8: the reviewer sees the request and the analysed columns.
            # In Pro mode it runs on the same Pro model as the draft: a
            # weaker model rewriting a Pro draft undid what the user paid for.
            for delta in service.stream(
                    'pro_max' if pro_mode else 'lite',
                    prompts.validation(target_language, filename, draft_code,
                                       user_prompt=user_prompt,
                                       columns_meta=metadata_summary),
                    temperature=0.0,
                    usage_out=validation_usage):
                validated += delta
                yield sse_event({'type': 'delta', 'text': delta})

            final_code = strip_code_fences(validated)
            if not final_code.strip():
                # D3-4: an empty validation result must never be moderated,
                # approved and delivered as the script; the except below
                # refunds and explains.
                raise llm.EmptyGenerationError()

            # Run-guard (0.4): re-moderate the model's OWN generated script, not
            # just the user's instruction (Gate 1). A jailbroken or misbehaving
            # model can emit network / env-exfiltration / shell code from a
            # benign-looking prompt, so moderating the instruction alone is not
            # enough. This upholds the same "every executed LLM-generated script
            # is moderated and recorded" invariant that /wrangle and the
            # edited-/run guard enforce. Default-deny: a malformed/ambiguous
            # verdict blocks (see moderation_blocked()).
            try:
                guard = service.generate(
                    'lite', prompts.code_moderation(final_code, target_language),
                    temperature=0.0, json_mode=True)
                guard_text = guard.text
            except llm.LLMOutputError as exc:
                # A provider block / empty verdict on the review call fails
                # closed exactly like an unparsable verdict.
                logger.warning("Run-guard code-moderation unusable: %s", exc)
                guard, guard_text = None, ''
            except Exception:
                logger.exception("Run-guard code-moderation failed in /chat")
                debit.refund()
                yield sse_event(debit.with_balance({
                    'type': 'error',
                    'message': 'Could not verify the generated script. Please try again.'}))
                return
            blocked, reason = moderation_blocked(guard_text)
            if blocked:
                # No runnable script was produced, so refund like the stream
                # failure path below rather than charge for a rejected result.
                debit.refund()
                yield sse_event(debit.with_balance({
                    'type': 'error',
                    'message': f'The generated script was rejected by the safety '
                               f'check. {reason}'}))
                return

            # Static pre-check (P1-1a): a non-LLM AST gate encoding the same
            # prohibitions as prompts.code_moderation, run as the LAST gate after
            # the (bypassable) LLM pass. Defense-in-depth ADDED to, never
            # replacing, the LLM guard above. On a block, refund like the guard
            # path and refuse to approve/save the script.
            static_blocked, static_reason = codecheck.check_code(
                final_code, target_language)
            if static_blocked:
                debit.refund()
                yield sse_event(debit.with_balance({
                    'type': 'error',
                    'message': f'The generated script was rejected by the safety '
                               f'check (static analysis). {static_reason}'}))
                return

            # Only a moderated script is remembered as approved; /run then
            # executes this (or a re-moderated edit of it) without re-moderating.
            # D6: remember what it was generated for, so /interpret can ground
            # on the user's question and the analysed columns server-side.
            storage.save_approved_script(
                final_code, target_language, source='chat',
                context={'prompt': user_prompt[:CONTEXT_PROMPT_MAX],
                         'filename': filename,
                         'columns': [str(c) for c in filtered_headers]
                         [:CONTEXT_COLUMNS_MAX],
                         'codebook': clamp_codebook(filtered_codebook,
                                                    CONTEXT_CODEBOOK_MAX)})

            usage = _sum_usage(mod.usage, selection_usage,
                               draft_usage, validation_usage, guard.usage)
            debit.delivered()
            yield sse_event(debit.with_balance({
                'type': 'done', 'code': final_code,
                'language': target_language, 'usage': usage}))
        except Exception as exc:
            logger.exception("Code generation stream failed")
            # The credit (if any) was debited before streaming began; the stream
            # produced no usable script, so refund it rather than charge for
            # nothing (P1-3). Best-effort: a refund failure must not mask the
            # original error the user needs to see. D3-4: a provider block,
            # refusal, empty or truncated reply says so instead of the generic
            # message.
            debit.refund()
            yield sse_event(debit.with_balance(llm_error_payload(
                exc, 'Code generation failed. Please try again.')))
        finally:
            # D1-15: a client disconnect closes this generator with
            # GeneratorExit, which the except above never sees. Anything not
            # delivered is refunded (at most once).
            debit.refund()

    # attach(): a stream closed before its first step runs none of its body,
    # so the response's close hook refunds that case.
    return debit.attach(sse_stream(generate))


# ---------------------------------------------------------------------------
# /run — guarded local execution (0.4 + Tier 0 sandbox + 5.2 multi-plot)
# ---------------------------------------------------------------------------

@bp.route('/run', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_run)
def run_code():
    data = request_json()
    # D1-12: validate every field BEFORE anything is persisted, so a bad
    # language can no longer be saved into the approved store and break /export.
    code = str_field(data, 'code')
    language = language_field(data)
    filename = dataset_filename_field(data)

    if not code:
        return json_error('No code provided.')
    # D4-29: an unchanged script runs under the language it was generated and
    # approved for, even if the dropdown was switched afterwards (Python code
    # under Rscript only produced a confusing error). Code approved for
    # neither language keeps the requested one and is reviewed for it below.
    language = storage.approved_language(code, language)

    # P2-2: membership over a hash-keyed store, not equality against one slot,
    # so a /wrangle that saved its own transform does not force a spurious
    # re-moderation of the unchanged, already-validated /chat script.
    # D1-7: approval is per (language, code) -- a script reviewed as Python
    # must be re-moderated before it may run under Rscript.
    # D4-10: a script restored into a fresh session (empty approved store) is
    # treated exactly like an edited one -- LLM code moderation (default-deny,
    # failing closed) and then the static check below -- instead of a hard
    # 403, so a reopened editor or a pasted export can run again.
    if not storage.is_approved_script(code, language):
        # User-edited or restored script (5.4): re-moderate first (0.4b).
        denied, _verdict = moderation_gate(
            llm.get_service(), prompts.code_moderation(code, language),
            denied='Edited script rejected by the safety check.',
            unavailable='Could not verify the edited script. Please try again.',
            log_message="Run-guard moderation failed")
        if denied is not None:
            return denied
        storage.save_approved_script(code, language, source='run')

    dataset_path = None
    dataset_name = None
    if filename:
        dataset_path = storage.active_dataset_path(filename)
        dataset_name = os.path.basename(storage.resolve_path(filename) or '') or None

    # Static pre-check (P1-1a): the FINAL, non-LLM gate before execution, for
    # both the approved-from-chat and the edited-and-re-moderated paths. This
    # AST denylist is defense-in-depth added to the LLM code_moderation above,
    # not a replacement — an LLM gate is prompt-injection bypassable and the
    # default subprocess sandbox has no network/filesystem isolation.
    static_blocked, static_reason = codecheck.check_code(code, language)
    if static_blocked:
        return json_error(
            f'Script rejected by the safety check (static analysis). '
            f'{static_reason}', 403)

    cfg = _cfg()
    result = sandbox.run_in_sandbox(
        code, language, dataset_path=dataset_path, dataset_name=dataset_name,
        timeout=cfg.exec_timeout, memory_mb=cfg.exec_memory_mb,
        output_limit=cfg.exec_output_limit, mode=cfg.sandbox_mode,
        runner_image=cfg.runner_image, work_root=cfg.sandbox_work_root)

    storage.save_last_run(result.output, result.plots,
                          script=code, language=language,
                          returncode=result.returncode,
                          timed_out=result.timed_out)

    # L9/S5: a timeout is an executed failure, not a server error. The script
    # ran and was stopped; its outcome is recorded above, so the reply is a 200
    # with ``timed_out`` and whatever it printed before the stop.
    return jsonify({
        'output': result.output,
        'plots': result.plots,
        'plot': result.plots[0] if result.plots else None,  # back-compat
        'success': result.success,
        'returncode': result.returncode,
        'timed_out': bool(result.timed_out),
        'language': language,
    })


# ---------------------------------------------------------------------------
# /interpret — single-pass streamed interpretation + auto-debugging (5.11)
# ---------------------------------------------------------------------------

_FAILURE_RE = re.compile(
    r'Traceback \(most recent call last\)'
    r'|^Error( in .*)?:'            # R: "Error in f(x) : ..." / "Error: ..."
    r'|^Execution halted'           # R's closing line on a fatal error
    r'|^Execution timed out after', # sandbox timeout message
    re.MULTILINE)


CONTEXT_PROMPT_MAX = 4000       # the user's question kept with an approval
CONTEXT_COLUMNS_MAX = 300
CONTEXT_CODEBOOK_MAX = 20000
INTERPRET_COLUMNS_MAX = 40      # columns described to the interpreter
INTERPRET_CONTEXT_MAX = 12000   # chars of column context in that prompt


def _codebook_parts(codebook):
    """Split a /chat codebook (``{col: 'Nominal'}`` or the client's merged
    ``{col: {type, description}}``) into levels and descriptions."""
    levels, descriptions = {}, {}
    for col, value in (codebook or {}).items():
        if isinstance(value, dict):
            if isinstance(value.get('type'), str):
                levels[col] = value['type']
            if isinstance(value.get('description'), str):
                descriptions[col] = value['description']
        elif isinstance(value, str):
            levels[col] = value
    return levels, descriptions


def _analysis_context(record):
    """``(question, column_context, n_rows)`` for the script that produced the
    last run, from the server's own records (D6 quick win 5): the context
    /chat stored with the approval, the active dataset's true row count, and
    the analysed columns with their native value labels. An edited script has
    no context of its own, so it inherits the latest /chat generation's."""
    entry = None
    if record:
        entry = storage.approved_entry(record.get('code'),
                                       record.get('language') or 'Python')
    ctx = (entry or {}).get('context')
    if not isinstance(ctx, dict):
        latest = storage.load_approved_script(exclude_sources=('wrangle', 'run'))
        ctx = (latest or {}).get('context')
    if not isinstance(ctx, dict):
        return None, None, None
    question = clamp(ctx.get('prompt'), CONTEXT_PROMPT_MAX).strip() or None
    filename = ctx.get('filename')
    if not isinstance(filename, str) or not filename:
        return question, None, None
    try:
        df, _ = read_active_frame(filename)
    except Exception:
        logger.warning("Could not read the dataset for interpretation context")
        df = None
    if df is None:
        return question, None, None
    columns = [c for c in (ctx.get('columns') or []) if c in df.columns]
    column_context = None
    if columns:
        levels, descriptions = _codebook_parts(ctx.get('codebook'))
        meta = storage.load_dataset_meta(filename) or {}
        labels = meta.get('labels') if isinstance(meta.get('labels'), dict) else {}
        merged = {**{str(k): str(v) for k, v in labels.items() if v},
                  **descriptions}
        column_context = clamp(datatools.build_column_context(
            df[columns[:INTERPRET_COLUMNS_MAX]], levels, merged),
            INTERPRET_CONTEXT_MAX)
    return question, column_context, len(df)


def _looks_failed(output):
    """Text heuristic for a failed run, used only when no recorded outcome
    exists (legacy run dirs, or the client-copy fallback without a success
    flag). Matches line starts anywhere, so R errors after earlier output and
    the timeout message are caught (D3-14)."""
    return bool(_FAILURE_RE.search(output or ''))


@bp.route('/interpret', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def interpret_results():
    data = request_json()
    service = llm.get_service()

    # P2-7: prefer the server-persisted last-run artifacts over the client's
    # copy of the results. The client `output`/`plots` are spoofable (a caller
    # can POST fabricated "results"); the server recorded exactly what the
    # sandbox produced on the last /run, so when those exist we ground the
    # interpretation on them and never touch the client text. Only when NO
    # server run is on record do we fall back to the client copy, and then it
    # must pass the same moderation gate /chat and /converse use (this route is
    # otherwise a general-purpose LLM proxy on the operator's key).
    server_output, server_plot_paths = storage.last_run_artifacts()
    grounded = bool(server_output.strip() or server_plot_paths)

    if grounded:
        # The debug `code` must be server-side too: prefer the script that
        # actually PRODUCED this run (recorded next to the last-run artifacts),
        # falling back to the most-recent approved script. Both are server-side
        # and trusted; the client `code` field is arbitrary spoofable text and
        # nothing client-supplied may reach the model unmoderated on this path
        # (P2-1/P2-7). Preferring the last-run script keeps the debugged code in
        # sync with `final_output` after a post-run /wrangle poisons the shared
        # approved-script store's "most recent" slot.
        record = storage.last_run_script() or storage.load_approved_script()
        code = clamp((record or {}).get('code'), FREE_TEXT_MAX)
        final_output = clamp(server_output, FREE_TEXT_MAX)
        plots = []
        for path in server_plot_paths[:3]:
            try:
                with open(path, 'rb') as fh:
                    plots.append(base64.b64encode(fh.read()).decode('ascii'))
            except Exception:
                logger.warning("Skipping unreadable server plot for interpretation")
        # Derive the pass/fail branch from the trusted server record, not the
        # client-sent (spoofable) success flag. D1-2: the sandbox's real return
        # code / timeout flag decide; the text heuristic is only a fallback
        # for run dirs written before the outcome was persisted.
        status = storage.last_run_status()
        if status is not None:
            failed = status['returncode'] != 0 or status['timed_out']
        else:
            failed = _looks_failed(final_output)
        question, column_context, n_rows = _analysis_context(record)
    else:
        # P1-7: cap the client-supplied text and plot payloads before they reach
        # the prompt. Client `code` is allowed here only because the moderation
        # gate below covers it together with `output`.
        code = clamp(data.get('code'), FREE_TEXT_MAX)
        final_output = clamp(data.get('output'), FREE_TEXT_MAX)
        plots = data.get('plots')
        if plots is None:
            plots = [data.get('plot')] if data.get('plot') else []
        plots = clamp_plots(plots)
        if 'success' in data:
            failed = not data.get('success')
        else:
            failed = _looks_failed(final_output)
        question = column_context = n_rows = None

    if not final_output.strip() and not plots:
        return jsonify({'interpretation':
                        'No statistical output or plots generated to interpret.'})

    # Fallback path only: moderate the client-supplied text before it reaches
    # the model, failing closed exactly like /converse (P2-7).
    if not grounded:
        moderated = (final_output + '\n' + code).strip()
        denied, _mod = moderation_gate(
            service, prompts.moderation(moderated),
            log_message="Interpret moderation failed")
        if denied is not None:
            return denied

    prompt = prompts.interpret(final_output, bool(plots), failed=failed,
                               code=code if failed else None,
                               question=question,
                               column_context=column_context, n_rows=n_rows)

    contents = [prompt]
    if plots and not failed:
        for b64 in plots[:3]:
            try:
                contents.append(llm.MediaPart(
                    data=base64.b64decode(b64), mime_type='image/png'))
            except Exception:
                logger.warning("Skipping undecodable plot for interpretation")

    def generate():
        usage = {}
        try:
            text = ''
            for delta in service.stream('flash', contents, temperature=0.3,
                                        usage_out=usage):
                text += delta
                yield sse_event({'type': 'delta', 'text': delta})
            if grounded:
                # D3-2: /converse and /generate_report ground on this copy
                # instead of the client's.
                try:
                    storage.save_last_interpretation(text)
                except Exception:
                    logger.warning("Could not store the interpretation",
                                   exc_info=True)
            yield sse_event({'type': 'done', 'debug': failed,
                             'usage': _sum_usage(usage)})
        except Exception as exc:
            logger.exception("Interpretation stream failed")
            yield sse_event(llm_error_payload(
                exc, 'Failed to generate AI interpretation.'))

    return sse_stream(generate)


# ---------------------------------------------------------------------------
# /method_prompt — analysis catalog → tailored prompt (5.14)
# ---------------------------------------------------------------------------

@bp.route('/method_prompt', methods=['POST'])
@limiter.limit(lambda: _cfg().rate_limit_chat)
def method_prompt():
    data = request_json()
    filename = dataset_filename_field(data)
    method = dict_field(data, 'method')
    # D1-6: every field below reaches the paid method_prompt call.
    method_name = clamp(method.get('name'), STYLE_FIELD_MAX).strip()
    method_desc = clamp(method.get('description'), BACKGROUND_MAX)
    codebook = clamp_mapping(dict_field(data, 'codebook'))
    pdf_mapping = clamp_mapping(dict_field(data, 'pdf_mapping'))
    if not filename or not method_name:
        return json_error('Missing filename or method')

    try:
        df, _ = read_active_frame(filename)   # D3-7: full-frame statistics
    except Exception:
        logger.exception("method_prompt read failure")
        return json_error('Could not read dataset.', 500)
    if df is None:
        return json_error('Invalid filename')
    try:
        context = datatools.build_column_context(df, codebook, pdf_mapping)
        result = llm.get_service().generate(
            'flash',
            prompts.method_prompt(method_name, method_desc, context),
            temperature=0.4, json_mode=True)
        # D3-9/D3-10: tolerant parse, then a real shape check -- the client
        # pastes 'prompt' straight into the prompt box.
        payload = json_object_reply(result.text)
        method_text = payload.get('prompt')
        if not isinstance(method_text, str) or not method_text.strip():
            raise llm.MalformedReplyError(detail='method_prompt.prompt')
        rationale = payload.get('rationale')
        return jsonify({'status': 'success',
                        'prompt': method_text.strip()[:FREE_TEXT_MAX],
                        'rationale': (rationale.strip()[:BACKGROUND_MAX]
                                      if isinstance(rationale, str) else ''),
                        'usage': usage_breakdown(result.usage)})
    except Exception as exc:
        logger.exception("method_prompt failed")
        return llm_json_error(exc, 'Could not draft a prompt for that method.')
