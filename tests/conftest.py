"""Shared pytest fixtures.

A fake, deterministic LLM service is injected via ``llm.set_service`` so the
whole HTTP surface can be exercised without network access or API keys
(roadmap 2.1). Every test gets its own temp upload root and SQLite file, so
file-isolation and DB tests never bleed into each other.
"""
import json
import os

import pytest

# Force a clean, key-less testing environment before anything imports config.
os.environ['APP_ENV'] = 'testing'
os.environ.pop('GEMINI_API_KEY', None)

import llm  # noqa: E402
from config import Config  # noqa: E402

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
        ('academic writing assistant', 'report'),
        ('revising one part', 'report_revision'),
    ]

    def __init__(self):
        self.calls = []                 # (role, kind, text, priority) per call
        self.usage_totals = {}
        self._overrides = {}
        self._defaults = {
            'moderation': 'PASS',
            'code_moderation': 'PASS',
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
            'report': '# Report\n\nThe analysis shows a strong effect.',
            'report_revision': 'The revised passage.',
        }

    # -- configuration -----------------------------------------------------
    def set(self, kind, value):
        self._overrides[kind] = value

    def block(self, reason='Safety Violation'):
        self._overrides['moderation'] = f'BLOCK: {reason}'

    def block_code(self, reason='network access'):
        self._overrides['code_moderation'] = f'BLOCK: {reason}'

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
    def generate(self, role, contents, *, temperature=0.2, json_mode=False,
                 priority=False):
        text = self._flatten(contents)
        kind = self._kind(text)
        self.calls.append((role, kind, text, priority))
        self._track(role, kind)
        return llm.LLMResult(text=self._payload(kind),
                             usage={'model': role, 'input': 10, 'output': 5})

    def stream(self, role, contents, *, temperature=0.2, usage_out=None,
               priority=False):
        text = self._flatten(contents)
        kind = self._kind(text)
        self.calls.append((role, kind, text, priority))
        self._track(role, kind)
        payload = self._payload(kind)
        # Emit in two chunks to exercise delta accumulation.
        mid = max(1, len(payload) // 2)
        for chunk in (payload[:mid], payload[mid:]):
            if chunk:
                yield chunk
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
    from app import create_app
    application = create_app(config)
    llm.set_service(fake_llm)        # override the real service init_service made
    application.config['_FAKE_LLM'] = fake_llm
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


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
