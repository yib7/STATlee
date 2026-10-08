"""WSGI entry point.

Production server target: ``wsgi:app`` (gunicorn). Run locally with::

    python wsgi.py

This is the only place that loads ``.env`` and builds the app, so importing the
``statlee`` package (as the test suite does) stays free of side effects. Under
``APP_ENV=testing`` the ``.env`` file is never read.
"""
import os

from dotenv import load_dotenv

if os.environ.get('APP_ENV', '').strip().lower() != 'testing':
    load_dotenv()

from statlee.app import create_app  # noqa: E402  (after .env is loaded)

app = create_app()

if __name__ == '__main__':
    cfg = app.config['STATLEE']
    # Loopback unless BIND_HOST opts in to LAN exposure (D5-8).
    app.run(debug=False, host=cfg.bind_host, port=cfg.port)
