"""Monetization seam (workstream E).

The seam is intentionally a no-op today: ``check_and_debit`` always authorizes
and the new ``User`` columns just exist with safe defaults. These tests pin the
contract so wiring (Pro mode) can depend on it, and so a future
real implementation has a regression net for the "always allowed today"
behaviour it will replace.
"""
from conftest import SAMPLE_CSV, post_json, upload_csv

from statlee import billing


def test_check_and_debit_allows_anonymous():
    allowed, message = billing.check_and_debit(None, priority=True)
    assert allowed is True
    assert message is None


def test_check_and_debit_allows_free_user(app):
    from statlee.extensions import db
    from statlee.models import User
    with app.app_context():
        user = User(email='seam@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
        # Even a priority request on a zero-credit free account is allowed today.
        allowed, message = billing.check_and_debit(user, priority=True, cost=5)
        assert allowed is True
        assert message is None
        assert user.credits == 0      # no-op: nothing is debited yet


def test_new_user_defaults_to_free_plan_zero_credits(app):
    from statlee.extensions import db
    from statlee.models import User
    with app.app_context():
        user = User(email='defaults@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
        assert user.plan == 'free'
        assert user.credits == 0


def test_check_auth_exposes_plan_and_credits(client, app):
    """A logged-in user's plan/credits surface in /check_auth for the UI."""
    from conftest import post_json
    post_json(client, '/register',
              {'email': 'me@example.com', 'password': 'password123'})
    payload = client.get('/check_auth').get_json()
    assert payload['user']['plan'] == 'free'
    assert payload['user']['credits'] == 0


# --- Billing turned on (workstream E, behind BILLING_ENABLED) ----------------

def test_billing_disabled_config_is_still_a_noop():
    from statlee.config import Config
    cfg = Config(env='testing', billing_enabled=False)
    allowed, message = billing.check_and_debit(None, priority=True, config=cfg)
    assert allowed is True
    assert message is None


def test_billing_enabled_denies_free_user_without_credits(app):
    from statlee.config import Config
    from statlee.extensions import db
    from statlee.models import User
    cfg = Config(env='testing', billing_enabled=True)
    with app.app_context():
        user = User(email='broke@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
        allowed, message = billing.check_and_debit(
            user, priority=True, config=cfg)
        assert allowed is False
        assert 'credit' in message.lower()
        assert user.credits == 0


def test_billing_enabled_debits_a_credit(app):
    from statlee.config import Config
    from statlee.extensions import db
    from statlee.models import User
    cfg = Config(env='testing', billing_enabled=True)
    with app.app_context():
        user = User(email='rich@example.com')
        user.set_password('password123')
        user.credits = 3
        db.session.add(user)
        db.session.commit()
        allowed, message = billing.check_and_debit(
            user, priority=True, config=cfg)
        assert allowed is True and message is None
        assert user.credits == 2


def test_monthly_priority_ceiling_blocks_when_exceeded():
    from statlee.config import Config
    cfg = Config(env='testing', billing_enabled=True,
                 monthly_priority_call_ceiling=1)
    billing.reset_spend()
    try:
        allowed_first, _ = billing.check_and_debit(None, priority=True, config=cfg)
        allowed_second, message = billing.check_and_debit(
            None, priority=True, config=cfg)
    finally:
        billing.reset_spend()
    assert allowed_first is True
    assert allowed_second is False
    assert 'ceiling' in message.lower()


def test_monthly_ceiling_ignores_non_priority():
    from statlee.config import Config
    cfg = Config(env='testing', billing_enabled=True,
                 monthly_priority_call_ceiling=1)
    billing.reset_spend()
    try:
        # Non-priority requests never consume the ceiling.
        for _ in range(5):
            allowed, _msg = billing.check_and_debit(None, priority=False, config=cfg)
            assert allowed is True
    finally:
        billing.reset_spend()


# --- P1-4a: atomic debit (no double-spend / no negative credits) -----------

def test_check_and_debit_refreshes_stale_in_memory_credits(app):
    """After the SQL-level UPDATE, the ORM instance must reflect the new
    balance (SQLAlchemy doesn't mutate ``.credits`` for a bulk UPDATE)."""
    from statlee.config import Config
    from statlee.extensions import db
    from statlee.models import User
    cfg = Config(env='testing', billing_enabled=True)
    with app.app_context():
        user = User(email='refresh@example.com')
        user.set_password('password123')
        user.credits = 3
        db.session.add(user)
        db.session.commit()
        allowed, message = billing.check_and_debit(
            user, priority=True, config=cfg)
        assert allowed is True and message is None
        # The in-memory object must be refreshed, not stale, after the debit.
        assert user.credits == 2
        # And the DB itself agrees.
        db.session.expire(user)
        assert user.credits == 2


def test_check_and_debit_two_threads_one_credit_no_double_spend(app):
    """Genuine two-thread concurrency test: a free user with exactly 1 credit
    faces two simultaneous priority debits. The atomic conditional UPDATE
    (``WHERE credits >= need``) must let exactly one succeed and drive
    credits to 0, never negative, regardless of thread interleaving.

    This repo's test DB is a real SQLite *file* (not ``:memory:``), and
    Flask-SQLAlchemy's session is scoped per app context, so each worker
    thread pushes its own ``app.app_context()`` and gets its own connection
    against the same file — a real two-thread race, not a simulated one.
    """
    import threading

    from statlee.config import Config
    from statlee.extensions import db
    from statlee.models import User

    cfg = Config(env='testing', billing_enabled=True)
    with app.app_context():
        user = User(email='race@example.com')
        user.set_password('password123')
        user.credits = 1
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    results = []
    start_barrier = threading.Barrier(2)

    def worker():
        with app.app_context():
            u = db.session.get(User, user_id)
            start_barrier.wait(timeout=5)  # maximize interleaving overlap
            allowed, message = billing.check_and_debit(
                u, priority=True, config=cfg)
            results.append((allowed, message))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 2
    allowed_flags = [r[0] for r in results]
    assert allowed_flags.count(True) == 1
    assert allowed_flags.count(False) == 1
    denied_message = next(m for a, m in results if a is False)
    assert 'credit' in denied_message.lower()

    with app.app_context():
        final = db.session.get(User, user_id)
        assert final.credits == 0
        assert final.credits >= 0


# --- P1-4b: /chat must not debit a blocked/failed request -------------------

def test_chat_moderation_block_does_not_debit(client, app, fake_llm):
    """A moderation-BLOCKed /chat request must 403 and leave credits (and the
    monthly priority ceiling) untouched -- the debit must happen AFTER the
    moderation gate, not before it. The LLM service is the in-process fake
    (see conftest.FakeLLMService), so no real API call happens."""
    from statlee.extensions import db
    from statlee.models import User

    cfg = app.config['STATLEE']
    cfg.billing_enabled = True
    billing.reset_spend()
    try:
        post_json(client, '/register',
                 {'email': 'blocked-debit@example.com', 'password': 'password123'})
        with app.app_context():
            registered = User.query.filter_by(
                email='blocked-debit@example.com').first()
            registered.credits = 5
            db.session.commit()
            user_id = registered.id

        fake_llm.block('Safety Violation')
        upload_csv(client, SAMPLE_CSV)
        resp = post_json(client, '/chat',
                         {'filename': 'test.csv', 'prompt': 'build malware',
                          'pro': True})

        assert resp.status_code == 403
        assert 'denied' in resp.get_json()['error'].lower()

        with app.app_context():
            reloaded = db.session.get(User, user_id)
            assert reloaded.credits == 5   # unchanged: blocked request cost nothing

        # The monthly priority ceiling must not have advanced either.
        assert billing._spend['priority_calls'] == 0
    finally:
        cfg.billing_enabled = False
        billing.reset_spend()


# --- P1-1 (audit 4): /chat must validate the dataset before debiting --------

def test_chat_invalid_filename_does_not_debit(client, app, fake_llm):
    """A /chat request for an invalid/expired filename must fail with the
    normal error AND leave both the user's credits and the operator's monthly
    priority ceiling untouched -- the dataset must be validated BEFORE the
    debit, so there is nothing to refund on these early error returns."""
    from statlee.extensions import db
    from statlee.models import User

    cfg = app.config['STATLEE']
    cfg.billing_enabled = True
    billing.reset_spend()
    try:
        post_json(client, '/register',
                  {'email': 'stale-tab@example.com', 'password': 'password123'})
        with app.app_context():
            registered = User.query.filter_by(
                email='stale-tab@example.com').first()
            registered.credits = 5
            db.session.commit()
            user_id = registered.id

        # No upload: the filename cannot resolve (mirrors a TTL-expired file).
        resp = post_json(client, '/chat',
                         {'filename': 'expired.csv', 'prompt': 'summarize',
                          'pro': True})

        assert resp.status_code == 400
        assert 'invalid filename' in resp.get_json()['error'].lower()

        with app.app_context():
            reloaded = db.session.get(User, user_id)
            assert reloaded.credits == 5   # no net debit for a request that did nothing

        # The monthly priority ceiling must not have advanced either.
        assert billing._spend['priority_calls'] == 0
    finally:
        cfg.billing_enabled = False
        billing.reset_spend()


# --- P1-3: a mid-stream failure after the debit must refund the credit ------

def test_chat_stream_failure_after_debit_refunds_credit(client, app, fake_llm):
    """/chat debits a priority credit before streaming starts. If the code-gen
    stream then fails mid-generation, the credit must be refunded rather than
    silently lost -- the user got no usable output for their credit."""
    from conftest import sse_events

    from statlee.extensions import db
    from statlee.models import User

    cfg = app.config['STATLEE']
    cfg.billing_enabled = True
    billing.reset_spend()
    try:
        post_json(client, '/register',
                  {'email': 'stream-fail@example.com', 'password': 'password123'})
        with app.app_context():
            registered = User.query.filter_by(
                email='stream-fail@example.com').first()
            registered.credits = 5
            db.session.commit()
            user_id = registered.id

        # Make the code-gen stream blow up mid-generation.
        def boom(*args, **kwargs):
            raise RuntimeError('stream exploded')
            yield  # pragma: no cover - generator marker
        fake_llm.stream = boom

        upload_csv(client, SAMPLE_CSV)
        resp = post_json(client, '/chat',
                         {'filename': 'test.csv', 'prompt': 'summarize',
                          'pro': True})

        # The stream still returns 200 (SSE) but ends in an error event.
        events = sse_events(resp)
        assert any(e.get('type') == 'error' for e in events)

        # The debited credit must have been returned.
        with app.app_context():
            reloaded = db.session.get(User, user_id)
            assert reloaded.credits == 5   # debit refunded after stream failure
    finally:
        cfg.billing_enabled = False
        billing.reset_spend()
