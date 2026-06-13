"""Storage layer: path containment (1.1), identity isolation, and dataset
version control with undo/redo (5.16)."""
import os

import pandas as pd
from flask import session

import storage


def _req(app, sid='session-a'):
    """A request context with a fixed session id (stable identity)."""
    ctx = app.test_request_context()
    ctx.push()
    session['sid'] = sid
    return ctx


def test_resolve_path_rejects_empty(app):
    ctx = _req(app)
    try:
        assert storage.resolve_path('') is None
        assert storage.resolve_path(None) is None
    finally:
        ctx.pop()


def test_resolve_path_contains_traversal(app):
    """secure_filename + realpath keep traversal attempts inside the root."""
    ctx = _req(app)
    try:
        root = os.path.realpath(storage.identity_root())
        evil = storage.resolve_path('../../../../etc/passwd')
        assert evil is not None
        assert os.path.commonpath([evil, root]) == root
        assert 'passwd' in os.path.basename(evil)
    finally:
        ctx.pop()


def test_identity_isolation(app):
    """Two sessions get distinct roots and cannot see each other's files."""
    ctx_a = _req(app, 'sid-a')
    try:
        path_a = storage.resolve_path('data.csv')
        with open(path_a, 'w') as f:
            f.write('a')
        root_a = storage.identity_root()
    finally:
        ctx_a.pop()

    ctx_b = _req(app, 'sid-b')
    try:
        root_b = storage.identity_root()
        path_b = storage.resolve_path('data.csv')
        assert root_a != root_b
        assert path_a != path_b
        assert not os.path.exists(path_b)   # B's namespace is empty
    finally:
        ctx_b.pop()


def test_version_control_undo_redo(app):
    ctx = _req(app)
    try:
        # Seed an original dataset and register it.
        path = storage.resolve_path('study.csv')
        df0 = pd.DataFrame({'x': [1, 2, 3]})
        df0.to_csv(path, index=False)
        storage.register_dataset('study.csv')

        # v2: a transformed frame.
        df1 = pd.DataFrame({'x': [1, 2]})
        storage.add_dataset_version('study.csv', df1, 'Dropped a row')
        log = storage.dataset_changelog('study.csv')
        assert log['active'] == 2
        assert log['can_undo'] is True
        assert log['can_redo'] is False

        # Undo -> active back to v1, redo available.
        storage.shift_dataset_version('study.csv', 'undo')
        log = storage.dataset_changelog('study.csv')
        assert log['active'] == 1
        assert log['can_redo'] is True
        active = pd.read_csv(storage.active_dataset_path('study.csv'))
        assert len(active) == 3

        # Redo -> v2 again.
        storage.shift_dataset_version('study.csv', 'redo')
        active = pd.read_csv(storage.active_dataset_path('study.csv'))
        assert len(active) == 2
    finally:
        ctx.pop()


def test_new_edit_truncates_redo_branch(app):
    ctx = _req(app)
    try:
        path = storage.resolve_path('t.csv')
        pd.DataFrame({'x': [1, 2, 3]}).to_csv(path, index=False)
        storage.register_dataset('t.csv')
        storage.add_dataset_version('t.csv', pd.DataFrame({'x': [1, 2]}), 'v2')
        storage.shift_dataset_version('t.csv', 'undo')   # back to v1
        # A new edit from v1 should discard the old v2 (redo branch).
        storage.add_dataset_version('t.csv', pd.DataFrame({'x': [9]}), 'v2-new')
        log = storage.dataset_changelog('t.csv')
        assert log['active'] == 2
        assert log['can_redo'] is False
        assert log['versions'][-1]['instruction'] == 'v2-new'
    finally:
        ctx.pop()


def test_approved_script_roundtrip(app):
    ctx = _req(app)
    try:
        assert storage.load_approved_script() is None
        storage.save_approved_script("print('hi')", 'Python')
        loaded = storage.load_approved_script()
        assert loaded['code'] == "print('hi')"
        assert loaded['language'] == 'Python'
    finally:
        ctx.pop()


def test_reset_identity_clears_workspace(app):
    ctx = _req(app)
    try:
        path = storage.resolve_path('keep.csv')
        with open(path, 'w') as f:
            f.write('x')
        assert os.path.exists(path)
        storage.reset_identity()
        assert not os.path.exists(path)
    finally:
        ctx.pop()
