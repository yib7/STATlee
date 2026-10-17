"""WSGI entry point.

Production server target: ``wsgi:app`` (gunicorn). Run locally with::

    python wsgi.py

This is the only place that loads ``.env`` and builds the app, so importing the
``statlee`` package (as the test suite does) stays free of side effects. Under
``APP_ENV=testing`` the ``.env`` file is never read.
"""
import os
import sys

from dotenv import load_dotenv

if os.environ.get('APP_ENV', '').strip().lower() != 'testing':
    load_dotenv()

from statlee.app import create_app  # noqa: E402  (after .env is loaded)
from statlee.config import ConfigError  # noqa: E402


def _build_app():
    """Build the app; a configuration error ends ``python wsgi.py`` and the
    Flask CLI with one line naming the variable, not a traceback."""
    try:
        return create_app()
    except ConfigError as exc:
        if 'gunicorn' in sys.modules:
            raise          # gunicorn reports a failed app import itself
        sys.exit(f"STATlee cannot start: {exc}")


app = _build_app()

if __name__ == '__main__':
    cfg = app.config['STATLEE']
    # Loopback unless BIND_HOST opts in to LAN exposure.
    app.run(debug=False, host=cfg.bind_host, port=cfg.port)
