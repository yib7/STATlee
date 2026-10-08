"""Shared pytest fixtures.

A fake, deterministic LLM service is injected via ``llm.set_service`` so the
whole HTTP surface can be exercised without network access or API keys
(roadmap 2.1). Every test gets its own temp upload root and SQLite file, so
file-isolation and DB tests never bleed into each other.
"""
import json
import os

import pytest

# Force a clean, key-less testing environment before anything imports config
# (D5-14). Importing the package no longer loads .env, and
# PYTHON_DOTENV_DISABLED makes any later load_dotenv() call (wsgi, the flask
# CLI) a no-op too, so a developer's real .env can never reach the suite.
# Provider keys and provider/model selection are scrubbed so a value exported
# in the shell cannot steer results either.
os.environ['APP_ENV'] = 'testing'
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
SCRUBBED_ENV = (
    'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'LLM_PROVIDER',
    'MODEL_PRO', 'MODEL_PRO_MAX', 'MODEL_FLASH', 'MODEL_FLASH_LITE',
)
for _var in SCRUBBED_ENV:
    os.environ.pop(_var, None)

from statlee import llm  # noqa: E402
from statlee.config import Config  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Re-scrub per test, in case a test (or something it imports) put a
    provider key or model selection back into the environment."""
    monkeypatch.setenv('APP_ENV', 'testing')
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    for var in SCRUBBED_ENV:
        monkeypatch.delenv(var, raising=False)

# ---------------------------------------------------------------------------
# Fake LLM service
# ---------------------------------------------------------------------------

class FakeLLMService:
    """Drop-in replacement for ``llm.LLMService``.

    It classifies a prompt by a marker phrase and returns a canned response,
    so routes get well-formed JSON/text without a real model. Tests can
    override any response with ``fake.set(kind, value)`` and flip moderation
    to a block with ``fake.block('...')``.
    """

    MARKERS = [
        ('strict safety and relevance filter', 'moderation'),
        ('strict security reviewer', 'code_moderation'),
        ('feature selection engine', 'feature_selection'),
        ('Classify EVERY column', 'classify'),
        ('suggest 3 ready-to-use', 'suggest'),
        ('data wrangling engine', 'wrangle'),
        ('methods consultant', 'method_prompt'),
        ('data dictionary extractor', 'pdf_extract'),
        ('codebook builder', 'survey_extract'),
        ('senior code reviewer', 'validation'),
        ('expert social science data analyst', 'draft'),
        ('debugging assistant', 'interpret_debug'),
        ('data mentor', 'interpret'),
        ('analysis guide', 'converse_guide'),
        ('academic mentor', 'converse'),
        ('research drafting assistant', 'report_draft'),
        ('academic writing assistant', 'report'),
        ('revising one part', 'report_revision'),
    ]

    def __init__(self):
        self.calls = []                 # (role, kind, text) per call
        self.usage_totals = {}
        self._overrides = {}
        self._failures = {}             # kind -> exception to raise
        self._defaults = {
            'moderation': json.dumps({'decision': 'pass', 'reason': ''}),
            'code_moderation': json.dumps({'decision': 'pass', 'reason': ''}),
            'feature_selection': json.dumps({'required_columns': []}),
            'classify': json.dumps({}),
            'suggest': json.dumps(['Suggestion 1', 'Suggestion 2', 'Suggestion 3']),
            'wrangle': json.dumps({'code': 'df = df.dropna()',
                                   'summary': 'Dropped rows with missing values',
                                   'error': None}),
            'method_prompt': json.dumps({'prompt': 'Run an OLS regression of y on x.',
                                         'rationale': 'Both are continuous.'}),
            'pdf_extract': json.dumps({}),
            'survey_extract': json.dumps({}),
            'validation': "print('validated')",
            'draft': "print('drafted')",
            'interpret_debug': '### What went wrong\nA typo.',
            'interpret': '### Summary\nThe effect is **significant**.',
            'converse_guide': 'Try framing it as X predicts Y.',
            'converse': 'That p-value means the result is unlikely by chance.',
            'report_draft': 'DRAFT: question, findings (coef=1.2, p=0.01), gaps.',
            'report': '# Report\n\nThe analysis shows a strong effect.',
            'report_revision': 'The revised passage.',
        }

    # -- configuration -----------------------------------------------------
    def set(self, kind, value):
        self._overrides[kind] = value

    def block(self, reason='Safety Violation'):
        self._overrides['moderation'] = json.dumps(
            {'decision': 'block', 'reason': reason})

    def block_code(self, reason='network access'):
        self._overrides['code_moderation'] = json.dumps(
            {'decision': 'block', 'reason': reason})

    def fail(self, kind, exc):
        """Make every call of ``kind`` raise ``exc`` (a stream raises after
        its first chunk, as a mid-stream provider stop would)."""
        self._failures[kind] = exc

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _flatten(contents):
        if isinstance(contents, str):
            return contents
        return "\n".join(str(c) for c in contents)

    def _kind(self, text):
        for marker, kind in self.MARKERS:
            if marker in text:
                return kind
        return 'unknown'

    def _payload(self, kind):
        return self._overrides.get(kind, self._defaults.get(kind, 'OK'))

    def _track(self, role, kind):
        entry = self.usage_totals.setdefault(
            role, {'calls': 0, 'input': 0, 'output': 0})
        entry['calls'] += 1
        entry['input'] += 10
        entry['output'] += 5

    # -- LLMService surface ------------------------------------------------
    def generate(self, role, contents, *, temperature=0.2, json_mode=False):
        text = self._flatten(contents)
        kind = self._kind(text)
        self.calls.append((role, kind, text))
        self._track(role, kind)
        if kind in self._failures:
            raise self._failures[kind]
        return llm.LLMResult(text=self._payload(kind),
                             usage={'model': role, 'input': 10, 'output': 5})

    def stream(self, role, contents, *, temperature=0.2, usage_out=None):
        text = self._flatten(contents)
        kind = self._kind(text)
        self.calls.append((role, kind, text))
        self._track(role, kind)
        payload = self._payload(kind)
        # Emit in two chunks to exercise delta accumulation.
        mid = max(1, len(payload) // 2)
        for i, chunk in enumerate((payload[:mid], payload[mid:])):
            if i == 1 and kind in self._failures:
                raise self._failures[kind]
            if chunk:
                yield chunk
        if kind in self._failures:
            raise self._failures[kind]
        if usage_out is not None:
            usage_out.update({'model': role, 'input': 10, 'output': 5})

    def usage_snapshot(self):
        return {role: dict(v) for role, v in self.usage_totals.items()}


# ---------------------------------------------------------------------------
# App / client fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config(tmp_path):
    cfg = Config(
        env='testing',
        upload_root=str(tmp_path / 'uploads'),
        database_url='sqlite:///' + str(tmp_path / 'test.db').replace('\\', '/'),
        flask_secret_key='test-secret-key',
        rate_limit_enabled=False,
        csrf_enabled=True,
        accounts_enabled=True,
        require_login=False,
        file_ttl_seconds=7200,
    )
    cfg.validate()
    return cfg


@pytest.fixture
def fake_llm():
    return FakeLLMService()


@pytest.fixture
def app(config, fake_llm):
    from statlee.app import create_app
    application = create_app(config)
    llm.set_service(fake_llm)        # override the real service init_service made
    application.config['_FAKE_LLM'] = fake_llm
    yield application
    # Close the per-test SQLite connections (avoids ResourceWarning noise).
    from statlee.extensions import db
    with application.app_context():
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def reset_limiter_state():
    """Reset the module-level Flask-Limiter singleton before and after a test.

    ``statlee.extensions.limiter`` is a single ``Limiter`` instance shared by
    every ``create_app()`` call in the process. flask_limiter 4.1.1's
    ``init_app()`` only populates ``limit_manager._default_limits`` once (it's
    gated on ``if not self.limit_manager._default_limits and conf_limits``)
    and unconditionally rebuilds ``_storage`` on every call — so a later
    ``create_app()`` with a different ``RATELIMIT_DEFAULT`` won't pick it up
    unless that cached state is cleared first, and whatever this test sets
    would otherwise leak into every test that runs after it. Any test that
    enables rate limiting with a custom ``RATELIMIT_DEFAULT`` should request
    this fixture for isolation.
    """
    from statlee.extensions import limiter

    def _reset():
        limiter.limit_manager._default_limits = []
        limiter.initialized = False

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def csrf_token(client):
    """Prime a session and return its CSRF token."""
    client.get('/')
    with client.session_transaction() as sess:
        return sess['csrf_token']


def post_json(client, path, payload, token=None):
    headers = {}
    if token is None:
        token = csrf_token(client)
    if token:
        headers['X-CSRF-Token'] = token
    return client.post(path, json=payload, headers=headers)


def upload_csv(client, content, filename='test.csv'):
    """Upload an in-memory CSV via the real /upload route."""
    import io
    token = csrf_token(client)
    data = {'file': (io.BytesIO(content.encode('utf-8')), filename)}
    return client.post('/upload', data=data, content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token})


def sse_events(response):
    """Parse a finished SSE response body into a list of event dicts."""
    body = response.get_data(as_text=True)
    return [json.loads(line[len('data:'):].strip())
            for line in body.splitlines() if line.startswith('data:')]


SAMPLE_CSV = "age,income,group\n25,30000,A\n40,55000,B\n,42000,A\n31,,B\n"
