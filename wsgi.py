"""WSGI entry point.

Production server target: ``wsgi:app`` (gunicorn). Run locally with::

    python wsgi.py
"""
from statlee.app import app

if __name__ == '__main__':
    port = app.config['STATLEE'].port
    app.run(debug=False, host='0.0.0.0', port=port)
