"""Validation and storage-shape helpers for non-HAM-D staff assessments."""

from decimal import Decimal, InvalidOperation


SIMPLE_SCALE_CODES = {
    'phq9', 'sass-j', 'bdi-ii', 'sds', 'stai-trait', 'stai-state', 'dai-10',
}

STAFF_TIMING_LABELS = {
    'baseline': '治療前',
    'post': '治療後',
    **{f'tinkertory_{index}': f'{index}回目' for index in range(1, 8)},
}


def staff_timing_label(timing):
    """Return the staff-facing label for non-HAM-D assessment timings."""
    return STAFF_TIMING_LABELS.get(timing, timing)

DETAIL_FIELDS = {
    'who-das': [
        ('cognition', '認知'), ('mobility', '移動'), ('self_care', 'セルフケア'),
        ('interpersonal', '対人関係'), ('life_activities', '生活活動'),
        ('social_participation', '社会参加'),
    ],
    'bacs': [
        ('composite', 'composite'), ('verbal_memory', '言語性記憶'),
        ('working_memory', 'ワーキングメモリ'), ('motor_speed', '運動速度'),
        ('verbal_fluency', '言語流暢性'), ('attention', '注意'),
        ('executive_function', '実行機能'),
    ],
}

COPM_FIELDS = ('importance', 'performance', 'satisfaction')
COPM_FIELD_LABELS = {
    'importance': '重要度', 'performance': '遂行度', 'satisfaction': '満足度',
}
SIX_MWT_VITAL_FIELDS = (
    ('blood_pressure', '血圧', 'text'), ('pulse', '脈拍', 'number'),
    ('spo2', 'SpO2', 'number'),
)
SIX_MWT_SUBJECTIVE_FIELDS = (
    ('knee_pain', '膝の疼痛', 'number'),
    ('dyspnea', '呼吸困難感', 'number'), ('leg_fatigue', '下肢疲労感', 'number'),
)
TINKERTOY_FIELDS = (
    ('pieces', 'ピース数', 'number'), ('time', '時間', 'number'),
    ('work_name', '作品名', 'text'), ('composition', '1構成', 'number'),
    ('parts_used', '2使用部品数', 'number'), ('naming', '3名称', 'number'),
    ('mobility', '4可動性', 'number'), ('three_dimensionality', '5立体性', 'number'),
    ('stability', '6安定性', 'number'), ('errors', '7誤り', 'number'),
    ('complexity', '複雑さ', 'number'), ('process_score', '作成プロセス', 'number'),
    ('total', '総合計', 'number'), ('z_score', 'Z-score', 'number'),
)


def parse_optional_number(value, integer=False):
    value = (value or '').strip()
    if not value:
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError('数値を入力してください。')
    if integer:
        if number != number.to_integral_value():
            raise ValueError('整数を入力してください。')
        return int(number)
    return int(number) if number == number.to_integral_value() else float(number)


def parse_simple_score(value):
    score = parse_optional_number(value, integer=True)
    if score is not None and score < 0:
        raise ValueError('0以上の整数を入力してください。')
    return score


def _detail_value(request, key, kind='number'):
    value = request.POST.get(key, '')
    if kind == 'text':
        return value.strip() or None
    return parse_optional_number(value)


def build_detail_scores(request, scale_code):
    if scale_code == 'who-das':
        scores = {key: _detail_value(request, key) for key, _label in DETAIL_FIELDS[scale_code]}
        scores['total'] = sum(value for value in scores.values() if value is not None)
        return scores
    if scale_code == 'bacs':
        return {key: _detail_value(request, key) for key, _label in DETAIL_FIELDS[scale_code]}
    if scale_code == 'copm':
        return {
            'items': [
                {
                    'work_name': _detail_value(request, f'work_name_{index}', 'text'),
                    **{field: _detail_value(request, f'{field}_{index}') for field in COPM_FIELDS},
                }
                for index in range(1, 4)
            ]
        }
    if scale_code == '6mwt':
        scores = {'vitals': {}, 'walking_distance': None, 'subjective': {}}
        for phase in ('before', 'after'):
            scores['vitals'][phase] = {
                key: _detail_value(request, f'{phase}_{key}', kind)
                for key, _label, kind in SIX_MWT_VITAL_FIELDS
            }
            scores['subjective'][phase] = {
                key: _detail_value(request, f'{phase}_{key}', kind)
                for key, _label, kind in SIX_MWT_SUBJECTIVE_FIELDS
            }
        scores['walking_distance'] = _detail_value(request, 'walking_distance')
        return scores
    if scale_code == 'tinkertory-test':
        return {key: _detail_value(request, key, kind) for key, _label, kind in TINKERTOY_FIELDS}
    return {}


def summary_for_record(scale_code, scores):
    if not scores:
        return '未評価'
    if scale_code == 'who-das' and scores.get('total') is not None:
        return f"合計 {scores['total']}"
    if scale_code == 'bacs' and scores.get('composite') is not None:
        return f"composite {scores['composite']}"
    if scale_code == 'tinkertory-test' and scores.get('total') is not None:
        return f"総合計 {scores['total']}"
    if scale_code == '6mwt' and scores.get('after', {}).get('walking_distance') is not None:
        return f"歩行距離 {scores['after']['walking_distance']}"
    if scale_code == '6mwt' and scores.get('walking_distance') is not None:
        return f"歩行距離 {scores['walking_distance']}"
    return '入力済'


def detail_form_values(scale_code, scores):
    values = {}
    if scale_code in {'who-das', 'bacs'}:
        return scores or {}
    if scale_code == 'copm':
        for index, item in enumerate((scores or {}).get('items', []), start=1):
            for key in ('work_name',) + COPM_FIELDS:
                values[f'{key}_{index}'] = item.get(key)
        return values
    if scale_code == '6mwt':
        for phase in ('before', 'after'):
            for key, _label, _kind in SIX_MWT_VITAL_FIELDS + SIX_MWT_SUBJECTIVE_FIELDS:
                values[f'{phase}_{key}'] = (scores or {}).get('vitals', {}).get(phase, {}).get(key)
                if values[f'{phase}_{key}'] is None:
                    values[f'{phase}_{key}'] = (scores or {}).get(phase, {}).get(key)
        values['walking_distance'] = (scores or {}).get('walking_distance')
        if values['walking_distance'] is None:
            values['walking_distance'] = (scores or {}).get('after', {}).get('walking_distance')
        return values
    if scale_code == 'tinkertory-test':
        return scores or {}
    return values


def detail_form_context(scale_code, scores):
    values = detail_form_values(scale_code, scores)
    if scale_code in {'who-das', 'bacs'}:
        return {'fields': [
            {'key': key, 'label': label, 'value': values.get(key)}
            for key, label in DETAIL_FIELDS[scale_code]
        ]}
    if scale_code == 'copm':
        return {'copm_items': [{
            'index': index,
            'work_name': values.get(f'work_name_{index}'),
            'values': [{'key': key, 'label': COPM_FIELD_LABELS[key], 'value': values.get(f'{key}_{index}')} for key in COPM_FIELDS],
        } for index in range(1, 4)]}
    if scale_code == '6mwt':
        return {'phases': [{
            'key': phase,
            'label': label,
            'fields': [
                {'key': f'{phase}_{key}', 'label': field_label, 'kind': kind, 'value': values.get(f'{phase}_{key}')}
                for key, field_label, kind in SIX_MWT_VITAL_FIELDS + SIX_MWT_SUBJECTIVE_FIELDS
            ],
        } for phase, label in (('before', '開始前'), ('after', '終了時'))],
            'walking_distance': values.get('walking_distance')}
    if scale_code == 'tinkertory-test':
        return {'fields': [
            {'key': key, 'label': label, 'kind': kind, 'value': values.get(key)}
            for key, label, kind in TINKERTOY_FIELDS
        ]}
    return {}