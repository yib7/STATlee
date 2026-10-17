"""Email fields are length-checked before the format regex (Phase 4, 4.4).

``EMAIL_RE`` backtracks quadratically on a long run of dots, so an unbounded
address let one public request pin a worker for minutes. An address longer
than RFC 5321's 254 characters is refused before the regex runs.
"""
import time

import pytest
from conftest import post_json

from statlee.routes import auth

# 60,000 dots took ~10 s in EMAIL_RE before the cap; the cap makes it instant.
SLOW_EMAIL = 'a@' + '.' * 60_000 + '@'


@pytest.mark.parametrize('path', ['/register', '/request_password_reset'])
def test_long_email_is_refused_without_running_the_regex(client, path):
    started = time.perf_counter()
    resp = post_json(client, path, {'email': SLOW_EMAIL,
                                    'password': 'long-enough-password'})
    elapsed = time.perf_counter() - started
    assert elapsed < 2, f'{path} took {elapsed:.1f}s on a long email'
    if path == '/register':
        assert resp.status_code == 400
    else:
        # The reset request stays a generic 200 (no enumeration).
        assert resp.status_code == 200


def test_email_at_the_limit_still_registers(client):
    local = 'a' * 64
    domain = ('b' * 60 + '.') * 3 + 'org'
    email = f'{local}@{domain}'
    assert len(email) <= auth.EMAIL_MAX
    resp = post_json(client, '/register', {'email': email,
                                           'password': 'long-enough-password'})
    assert resp.status_code == 201


def test_email_over_the_limit_is_refused(client):
    email = 'a' * 250 + '@example.org'
    assert len(email) > auth.EMAIL_MAX
    resp = post_json(client, '/register', {'email': email,
                                           'password': 'long-enough-password'})
    assert resp.status_code == 400
