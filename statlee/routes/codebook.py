"""Survey-native codebook routes.

``GET /codebook?filename=``         the merged codebook of a dataset
``PUT /codebook``                   partial or full update, fields become user
``POST /codebook/import``           a codebook JSON document (fields -> user)
``GET /codebook/export?filename=``  the codebook JSON document as a download

Every route resolves the dataset under the caller's own identity root (an
unknown or another identity's dataset is a 404), validates and clamps the
payload in ``statlee.codebook`` (an invalid one is a 400), and the writes are
CSRF-protected by the app-wide hook like every other JSON write.
"""
import json
import logging
import os

from flask import Blueprint, Response, jsonify, request
from werkzeug.utils import secure_filename

from .. import codebook as cb
from .. import storage
from . import (
    dataset_filename_field,
    json_error,
    register_field_errors,
    request_json,
)

logger = logging.getLogger('statlee.codebook')

bp = Blueprint('codebook', __name__)
register_field_errors(bp)
bp.register_error_handler(
    cb.CodebookStorageError,
    lambda e: json_error("The dataset's metadata could not be read, so the "
                         'codebook was not saved. Upload the dataset again.',
                         500))

bp.register_error_handler(cb.CodebookTooLarge,
                          lambda e: json_error(str(e), 413))

UNKNOWN_DATASET = 'Unknown dataset.'
OPTIONS = {'types': list(cb.TYPES), 'roles': list(cb.ROLES),
           'sources': list(cb.SOURCES) + [cb.DEFAULT_SOURCE],
           'fields': list(cb.FIELDS)}


def _dataset(data):
    """``(filename, columns)`` for the dataset a request names; a missing name
    is a 400 response, an unknown one a 404 response (returned as the second
    item)."""
    filename = dataset_filename_field(data)
    if not filename:
        return None, json_error('Missing filename')
    columns = cb.dataset_columns(filename)
    if columns is None:
        return None, json_error(UNKNOWN_DATASET, 404)
    return filename, columns


def _payload(filename, columns, **extra):
    layers = cb.load_layers(filename)
    meta = storage.load_dataset_meta(filename) or {}
    sha = meta.get('sha256')
    body = {
        'status': 'success',
        'filename': os.path.basename(storage.resolve_path(filename) or '')
        or filename,
        'sha256': sha if isinstance(sha, str) else None,
        'dataset_version': storage.active_version_number(filename),
        'has_codebook': cb.has_entries(layers),
        # jsonify sorts object keys: ``columns`` carries the dataset order.
        'columns': list(columns),
        'codebook': cb.merge(layers, columns),
        'options': OPTIONS,
    }
    body.update(extra)
    return jsonify(body)


@bp.route('/codebook', methods=['GET'])
def get_codebook():
    filename, columns = _dataset(request.args.to_dict())
    if filename is None:
        return columns
    return _payload(filename, columns)


@bp.route('/codebook', methods=['PUT'])
def put_codebook():
    data = request_json()
    filename, columns = _dataset(data)
    if filename is None:
        return columns
    try:
        updates = cb.parse_update(data.get('codebook'), columns)
    except cb.CodebookError as exc:
        return json_error(str(exc))
    cb.apply(filename, 'user', updates)
    return _payload(filename, columns)


@bp.route('/codebook/import', methods=['POST'])
def import_codebook():
    data = request_json()
    filename, columns = _dataset(data)
    if filename is None:
        return columns
    try:
        updates, ignored = cb.parse_import(data.get('codebook'), columns)
    except cb.CodebookError as exc:
        return json_error(str(exc))
    cb.apply(filename, 'user', updates)
    return _payload(filename, columns, imported=len(updates),
                    ignored=ignored[:200])


@bp.route('/codebook/export', methods=['GET'])
def export_codebook():
    filename, columns = _dataset(request.args.to_dict())
    if filename is None:
        return columns
    doc = cb.export_document(filename)
    if doc is None:
        return json_error(UNKNOWN_DATASET, 404)
    stem = os.path.splitext(secure_filename(filename))[0] or 'dataset'
    return Response(
        json.dumps(doc, indent=2, ensure_ascii=False),
        mimetype='application/json',
        headers={'Content-Disposition':
                 f'attachment; filename="{stem}_codebook.json"'})
