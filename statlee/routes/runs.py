"""Session workbook routes (SP1): list, view, rename and star runs.

Every lookup goes through ``storage``'s run store, which validates the id
(``^r[0-9]{4,6}$``) and resolves it under the caller's own identity root, so
a malformed, unknown, evicted or another identity's id is a 404. PATCH is
CSRF-protected by the app-wide hook like every other mutating JSON route.
"""
import base64
import logging

from flask import Blueprint, jsonify

from .. import methods as methods_card
from .. import storage
from . import (
    RunNotFound,
    json_error,
    register_field_errors,
    request_json,
)

logger = logging.getLogger('statlee.runs')

bp = Blueprint('runs', __name__)
register_field_errors(bp)


def _meta_str(meta, key):
    value = meta.get(key)
    return value if isinstance(value, str) else None


@bp.route('/runs', methods=['GET'])
def list_runs():
    return jsonify({'runs': storage.list_runs(),
                    'latest': storage.latest_run_id()})


@bp.route('/runs/<run_id>', methods=['GET'])
def get_run(run_id):
    run = storage.load_run(run_id)
    if run is None:
        raise RunNotFound(run_id)
    meta = run['meta']
    script = run['script'] or {}
    status = run['status'] or {}
    plots = []
    for path in run['plots']:
        try:
            with open(path, 'rb') as fh:
                plots.append(base64.b64encode(fh.read()).decode('ascii'))
        except OSError:
            logger.warning("Skipping unreadable plot of run %s", run_id)
    version = meta.get('dataset_version')
    return jsonify({
        'run_id': run_id,
        'name': _meta_str(meta, 'name'),
        'created_at': _meta_str(meta, 'created_at'),
        'prompt': _meta_str(meta, 'prompt'),
        'prompt_inherited': bool(meta.get('prompt_inherited')),
        'filename': _meta_str(meta, 'filename'),
        'dataset_version': version if isinstance(version, int) else None,
        'dataset_sha256': _meta_str(meta, 'dataset_sha256'),
        'model': _meta_str(meta, 'model'),
        'role': _meta_str(meta, 'role'),
        'starred': bool(meta.get('starred')),
        # SP3: the stored card, or one parsed on the fly for a run saved
        # before the methods card existed (``methods: null``).
        'methods': methods_card.for_run(meta, run['output']),
        'output': run['output'],
        'code': script.get('code'),
        'language': script.get('language'),
        'returncode': status.get('returncode'),
        'timed_out': bool(status.get('timed_out')),
        'interpretation': run['interpretation'],
        'plots': plots,
        'is_latest': run_id == storage.latest_run_id(),
    })


@bp.route('/runs/<run_id>', methods=['PATCH'])
def patch_run(run_id):
    data = request_json()
    changes = {}
    if 'name' in data:
        name = data['name']
        if name is not None and not isinstance(name, str):
            return json_error("'name' must be a string.")
        name = (name or '').strip()
        if len(name) > storage.RUN_NAME_MAX:
            return json_error(
                f"'name' must be at most {storage.RUN_NAME_MAX} characters.")
        # One line of plain text: control characters (newlines included)
        # become spaces.
        changes['name'] = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c
                                  for c in name).strip()
    if 'starred' in data:
        if not isinstance(data['starred'], bool):
            return json_error("'starred' must be true or false.")
        changes['starred'] = data['starred']
    if not changes:
        return json_error("Nothing to update: send 'name' and/or 'starred'.")
    if storage.update_run_meta(run_id, **changes) is None:
        raise RunNotFound(run_id)
    summary = storage.run_summary(run_id)
    if summary is None:          # evicted between the update and the read
        raise RunNotFound(run_id)
    return jsonify(summary)
