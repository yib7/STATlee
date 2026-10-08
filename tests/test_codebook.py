"""SP2 survey-native codebook: schema, validation, clamping, merge precedence
and prompt rendering (``statlee.codebook``, pure functions only; the routes
are covered in test_codebook_routes.py)."""
import json

import pytest

from statlee import codebook as cb


def _layers(**by_source):
    layers = cb.empty_layers()
    for source, cols in by_source.items():
        layers[source] = cols
    return layers


# ---------------------------------------------------------------------------
# Schema and validation
# ---------------------------------------------------------------------------

def test_default_entry_shape():
    entry = cb.default_entry()
    assert entry == {
        'type': None, 'label': '', 'value_labels': {}, 'missing_codes': [],
        'role': 'analyze', 'scale_group': None, 'source': 'default',
        'user_fields': [], 'field_sources': {}}


def test_clean_fields_normalises_and_clamps():
    out = cb.clean_fields({
        'type': ' Ordinal ', 'label': '  Trust in parliament  ',
        'value_labels': {1.0: 'Not at all', '5': 'Completely', ' ': 'drop'},
        'missing_codes': [-9, '-8', -9, 98.0],
        'role': 'ANALYZE', 'scale_group': '  trust ',
    })
    assert out == {
        'type': 'ordinal', 'label': 'Trust in parliament',
        'value_labels': {'1': 'Not at all', '5': 'Completely'},
        'missing_codes': ['-9', '-8', '98'],
        'role': 'analyze', 'scale_group': 'trust'}


def test_clean_fields_caps_every_field():
    out = cb.clean_fields({
        'label': 'x' * (cb.LABEL_MAX + 50),
        'value_labels': {str(i): 'v' * (cb.VALUE_TEXT_MAX + 9)
                         for i in range(cb.VALUE_LABELS_MAX + 30)},
        'missing_codes': [str(i) * 3 for i in range(cb.MISSING_CODES_MAX + 30)],
        'scale_group': 'g' * (cb.SCALE_GROUP_MAX + 10),
    })
    assert len(out['label']) == cb.LABEL_MAX
    assert len(out['value_labels']) == cb.VALUE_LABELS_MAX
    assert all(len(v) == cb.VALUE_TEXT_MAX for v in out['value_labels'].values())
    assert len(out['missing_codes']) == cb.MISSING_CODES_MAX
    assert len(out['scale_group']) == cb.SCALE_GROUP_MAX
    long_code = cb.clean_fields({'value_labels': {'c' * 500: 'x'}})
    assert all(len(k) <= cb.CODE_MAX for k in long_code['value_labels'])


@pytest.mark.parametrize('fields', [
    {'type': 'interval'},
    {'type': 3},
    {'role': 'outcome'},
    {'label': ['a']},
    {'value_labels': ['1=a']},
    {'value_labels': {'1': {'nested': 1}}},
    {'missing_codes': '-9'},
    {'missing_codes': [{'x': 1}]},
    {'missing_codes': [True]},
    {'scale_group': 5},
    {'type': None},
    {'colour': 'red'},
])
def test_clean_fields_rejects_invalid_values(fields):
    with pytest.raises(cb.CodebookError):
        cb.clean_fields(fields)


def test_clean_fields_ignores_read_only_keys_and_allows_null_group():
    out = cb.clean_fields({'source': 'user', 'user_fields': ['label'],
                           'field_sources': {}, 'scale_group': None})
    assert out == {'scale_group': None}


def test_clean_fields_lenient_skips_nulls_and_unknown_keys():
    out = cb.clean_fields({'type': None, 'label': 'Age', 'colour': 'red',
                           'scale_group': None}, lenient=True)
    assert out == {'label': 'Age'}


# ---------------------------------------------------------------------------
# Merge precedence: user > native > pdf > llm, per field
# ---------------------------------------------------------------------------

def test_merge_precedence_is_per_field():
    layers = _layers(
        llm={'q1': {'type': 'continuous', 'label': 'llm label'}},
        pdf={'q1': {'label': 'pdf label', 'value_labels': {'1': 'pdf one'}}},
        native={'q1': {'value_labels': {'1': 'Yes', '2': 'No'}}},
        user={'q1': {'type': 'binary'}},
    )
    entry = cb.merge(layers, ['q1'])['q1']
    assert entry['type'] == 'binary'                    # user beats llm
    assert entry['label'] == 'pdf label'                # pdf beats llm
    assert entry['value_labels'] == {'1': 'Yes', '2': 'No'}  # native beats pdf
    assert entry['source'] == 'user'
    assert entry['user_fields'] == ['type']
    assert entry['field_sources'] == {'type': 'user', 'label': 'pdf',
                                      'value_labels': 'native'}


def test_merge_fills_defaults_and_follows_column_order():
    layers = _layers(llm={'b': {'type': 'nominal'}, 'gone': {'type': 'text'}})
    merged = cb.merge(layers, ['b', 'new_col'])
    assert list(merged) == ['b', 'new_col']             # removed column dropped
    assert merged['b']['source'] == 'llm'
    assert merged['new_col'] == cb.default_entry()      # added column defaulted


def test_merge_source_is_highest_contributing_layer():
    layers = _layers(llm={'a': {'type': 'continuous'}},
                     native={'a': {'label': 'Age'}})
    assert cb.merge(layers, ['a'])['a']['source'] == 'native'


def test_user_field_survives_a_later_seed():
    layers = _layers(user={'a': {'label': 'My label'}})
    cb.apply_to_layers(layers, 'llm', {'a': {'type': 'nominal'}})
    cb.apply_to_layers(layers, 'native', {'a': {'label': 'Native label'}})
    cb.apply_to_layers(layers, 'pdf', {'a': {'label': 'PDF label'}})
    entry = cb.merge(layers, ['a'])['a']
    assert entry['label'] == 'My label'
    assert entry['type'] == 'nominal'
    assert entry['user_fields'] == ['label']


def test_apply_to_layers_merges_fields_within_a_source():
    layers = cb.empty_layers()
    cb.apply_to_layers(layers, 'user', {'a': {'label': 'A'}})
    cb.apply_to_layers(layers, 'user', {'a': {'role': 'id'}})
    assert layers['user']['a'] == {'label': 'A', 'role': 'id'}
    with pytest.raises(ValueError):
        cb.apply_to_layers(layers, 'default', {'a': {'label': 'x'}})


def test_has_entries():
    assert not cb.has_entries(cb.empty_layers())
    assert cb.has_entries(_layers(llm={'a': {'type': 'nominal'}}))


def test_legacy_labels_become_the_native_layer():
    layers = cb.layers_from_meta({'sha256': 'x', 'labels': {'g': 'Gender'}})
    assert layers['native'] == {'g': {'label': 'Gender'}}
    fresh = cb.layers_from_meta({'labels': {'g': 'Gender'},
                                 'codebook': {'layers': cb.empty_layers()}})
    assert fresh['native'] == {}


def test_layers_from_meta_drops_corrupt_content():
    meta = {'codebook': {'layers': {'user': {'a': {'type': 'bogus',
                                                   'label': 'ok'},
                                             'b': 'not a dict'},
                                    'llm': ['nope']}}}
    layers = cb.layers_from_meta(meta)
    assert layers['user'] == {'a': {'label': 'ok'}}
    assert layers['llm'] == {}


# ---------------------------------------------------------------------------
# Request parsing (PUT and import)
# ---------------------------------------------------------------------------

def test_parse_update_validates_columns_and_fields():
    updates = cb.parse_update({'age': {'label': 'Age', 'role': 'id'}},
                              ['age', 'income'])
    assert updates == {'age': {'label': 'Age', 'role': 'id'}}
    with pytest.raises(cb.CodebookError):
        cb.parse_update({'nope': {'label': 'x'}}, ['age'])
    with pytest.raises(cb.CodebookError):
        cb.parse_update({'age': 'Continuous'}, ['age'])
    with pytest.raises(cb.CodebookError):
        cb.parse_update({}, ['age'])
    with pytest.raises(cb.CodebookError):
        cb.parse_update(['age'], ['age'])


def test_parse_import_accepts_export_document_and_bare_map():
    doc = {'format': cb.EXPORT_FORMAT, 'version': 1,
           'codebook': {'age': {'type': 'continuous', 'label': 'Age',
                                'source': 'llm', 'user_fields': [],
                                'scale_group': None},
                        'other': {'label': 'not in this dataset'}}}
    updates, ignored = cb.parse_import(doc, ['age'])
    assert updates == {'age': {'type': 'continuous', 'label': 'Age'}}
    assert ignored == ['other']
    bare, _ = cb.parse_import({'age': {'role': 'weight'}}, ['age'])
    assert bare == {'age': {'role': 'weight'}}


def test_parse_import_rejects_invalid_documents():
    with pytest.raises(cb.CodebookError):
        cb.parse_import('nope', ['age'])
    with pytest.raises(cb.CodebookError):
        cb.parse_import({'codebook': {'age': {'type': 'bogus'}}}, ['age'])
    with pytest.raises(cb.CodebookError):
        cb.parse_import({'codebook': {}}, ['age'])
    too_many = {f'c{i}': {} for i in range(cb.IMPORT_ENTRIES_MAX + 1)}
    with pytest.raises(cb.CodebookError):
        cb.parse_import(too_many, ['c0'])


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _entries():
    layers = _layers(
        native={'gender': {'label': 'Respondent gender',
                           'value_labels': {'1': 'Male', '2': 'Female'},
                           'missing_codes': ['-9']}},
        llm={'gender': {'type': 'nominal'}, 'age': {'type': 'continuous'}},
        user={'resp_id': {'role': 'id'}, 'w': {'role': 'weight'},
              'notes': {'role': 'ignore'},
              't1': {'scale_group': 'trust', 'type': 'ordinal'}},
    )
    return cb.merge(layers, ['gender', 'age', 'resp_id', 'w', 'notes', 't1',
                             'blank'])


def test_prompt_dict_is_compact_and_skips_default_entries():
    out = cb.prompt_dict(_entries())
    assert 'blank' not in out
    assert out['gender'] == {'type': 'nominal', 'label': 'Respondent gender',
                             'value_labels': {'1': 'Male', '2': 'Female'},
                             'missing_codes': ['-9']}
    assert out['age'] == {'type': 'continuous'}
    assert out['resp_id'] == {'role': 'id'}
    assert out['t1'] == {'type': 'ordinal', 'scale_group': 'trust'}
    json.dumps(out)


def test_prompt_dict_caps_value_labels():
    layers = _layers(native={'q': {'value_labels': {
        str(i): f'v{i}' for i in range(cb.PROMPT_VALUE_LABELS_MAX + 20)}}})
    out = cb.prompt_dict(cb.merge(layers, ['q']))
    assert len(out['q']['value_labels']) == cb.PROMPT_VALUE_LABELS_MAX + 1
    assert out['q']['value_labels']['...'] == '20 more'


def test_describe_mentions_labels_missing_roles_and_scale_groups():
    entries = _entries()
    gender = cb.describe(entries['gender'])
    assert 'Respondent gender' in gender
    assert '1=Male' in gender and '2=Female' in gender
    assert 'treat as missing' in gender and '-9' in gender
    assert 'exclude from modelling' in cb.describe(entries['resp_id'])
    assert 'exclude from modelling' in cb.describe(entries['notes'])
    assert 'survey weight' in cb.describe(entries['w'])
    assert 'trust' in cb.describe(entries['t1'])
    assert cb.describe(entries['blank']) == ''


def test_prompt_levels_and_descriptions():
    levels, descriptions = cb.prompt_levels_and_descriptions(_entries())
    assert levels['gender'] == 'Nominal' and levels['t1'] == 'Ordinal'
    assert 'blank' not in levels and 'blank' not in descriptions
    assert 'Respondent gender' in descriptions['gender']
    assert 'age' not in descriptions          # a type alone is not a description


def test_role_summary_names_weight_and_excluded_columns():
    summary = cb.role_summary(cb.prompt_dict(_entries()))
    assert 'w' in summary['weight']
    assert set(summary['exclude']) == {'resp_id', 'notes'}
    assert summary['scale_groups'] == {'trust': ['t1']}
