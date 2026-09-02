"""
Research CSV export service.

Generates three research-oriented CSVs (wide format for the summary, long
format for the per-session/per-event details):

- research_summary.csv:          1 row = 1 (card_id, course_number)
- research_treatment_detail.csv: 1 row = 1 TreatmentSession
- research_adverse_events.csv:   1 row = 1 SeriousAdverseEvent (joined with
                                  AdverseEventReport when available)

Column names always encode the assessment timing (e.g. HAMD_baseline_total17)
so a researcher can tell "what" and "when" from the header alone. Scale
columns are built dynamically from ScaleDefinition/TimingScaleConfig so a
newly added scale is picked up automatically without code changes.
"""

import csv
import io

from rtms_app.models import (
    AdverseEventReport,
    AssessmentRecord,
    Patient,
    ScaleDefinition,
    SeriousAdverseEvent,
    SideEffectCheck,
    TreatmentCourse,
    TimingScaleConfig,
    TreatmentSession,
)
from rtms_app.services.side_effect_schema import SIDE_EFFECT_ITEMS

MAX_PLANNED_SESSIONS = 30

# Scales whose timing is not tracked via TimingScaleConfig today (the
# assessment hub renders them with a hardcoded baseline/post pair instead).
OT_PRE_POST_SCALE_CODES = {'who-das', 'bacs', 'copm', '6mwt'}
TINKERTORY_SCALE_CODE = 'tinkertory-test'
TINKERTORY_TIMINGS = [f'tinkertory_{i}' for i in range(1, 8)]

# Cosmetic prefix for known scales; any future scale not listed here falls
# back to its upper-cased code so it is still exported automatically.
SCALE_PREFIX_OVERRIDES = {
    'hamd': 'HAMD', 'bacs': 'BACS', 'phq9': 'PHQ9', 'sass-j': 'SASSJ',
    'bdi-ii': 'BDI2', 'sds': 'SDS', 'stai-trait': 'STAITRAIT',
    'stai-state': 'STAISTATE', 'dai-10': 'DAI10', 'who-das': 'WHODAS',
    'copm': 'COPM', '6mwt': '6MWT', 'tinkertory-test': 'TINKERTORY',
}

BACS_SUBFIELDS = [
    ('composite', 'composite'), ('verbal_memory', 'verbal_memory'),
    ('working_memory', 'working_memory'), ('motor_speed', 'motor_speed'),
    ('verbal_fluency', 'verbal_fluency'), ('attention', 'attention'),
    ('executive_function', 'executive_function'),
]
WHODAS_SUBFIELDS = [
    ('cognition', 'cognition'), ('mobility', 'mobility'), ('self_care', 'self_care'),
    ('interpersonal', 'interpersonal'), ('life_activities', 'life_activities'),
    ('social_participation', 'social_participation'), ('total', 'total'),
]

# Real-world SideEffectCheck.rows use slightly different punctuation than the
# reference SIDE_EFFECT_ITEMS labels (half-width vs full-width parentheses,
# presence/absence of "の"), so both variants are mapped to the same key.
SIDE_EFFECT_LABEL_ALIASES = {
    '頭皮痛・刺激痛': 'scalp_pain',
    '顔面の不快感': 'facial_discomfort',
    '頸部痛・肩こり': 'neck_shoulder_pain',
    '頭痛（刺激後）': 'headache_post',
    '頭痛 (刺激後)': 'headache_post',
    'けいれん（部位・時間）': 'seizure',
    'けいれん (部位・時間)': 'seizure',
    '失神': 'syncope',
    '聴覚障害': 'hearing_issue',
    'めまい・耳鳴り': 'dizziness_tinnitus',
    '注意集中困難': 'attention_issue',
    '急性の気分変化（躁転など）': 'acute_mood_change',
    '急性気分変化 (躁転など)': 'acute_mood_change',
    'その他': 'other',
}


def _blank(value):
    """Missing/unassessed data must stay blank; 0 is a real measured value."""
    return '' if value is None else value


def scale_prefix(scale_code):
    return SCALE_PREFIX_OVERRIDES.get(scale_code, scale_code.upper().replace('-', '_'))


def _timings_for_scale(scale):
    if scale.code == TINKERTORY_SCALE_CODE:
        return TINKERTORY_TIMINGS
    configured = list(
        TimingScaleConfig.objects.filter(scale=scale, is_enabled=True)
        .order_by('display_order')
        .values_list('timing', flat=True)
    )
    if configured:
        return configured
    # OT scales (and any future scale) without explicit TimingScaleConfig rows
    # are evaluated at baseline/post, matching the assessment hub's fallback.
    return ['baseline', 'post']


def _get_record(patient, course_number, scale, timing):
    return AssessmentRecord.objects.filter(
        patient=patient, course_number=course_number, timing=timing, scale=scale,
    ).order_by('-date').first()


def _generic_total(record):
    if not record:
        return None
    scores = record.scores or {}
    if scores.get('total') is not None:
        return scores.get('total')
    return scores.get('score')


def _six_mwt_distance(record):
    if not record:
        return None
    scores = record.scores or {}
    if scores.get('walking_distance') is not None:
        return scores.get('walking_distance')
    return (scores.get('after') or {}).get('walking_distance')


def _copm_avg(record, field):
    """Mean of the entered COPM items for one field (importance/performance/satisfaction).

    No prior averaging logic exists elsewhere in the codebase; this is a
    straightforward arithmetic mean over items that have a value, rounded to
    2 decimals, blank when nothing was entered.
    """
    if not record:
        return ''
    items = (record.scores or {}).get('items') or []
    values = [item.get(field) for item in items if item.get(field) is not None]
    if not values:
        return ''
    return round(sum(values) / len(values), 2)


def _timing_columns(scale, timing):
    """Return [(column_key, extractor(record))] for one scale at one timing."""
    prefix = scale_prefix(scale.code)
    if scale.code == TINKERTORY_SCALE_CODE:
        col_prefix = f"{prefix}_{timing.split('_')[-1]}"
        return [
            (f'{col_prefix}_total', lambda r: _blank((r.scores or {}).get('total') if r else None)),
            (f'{col_prefix}_time', lambda r: _blank((r.scores or {}).get('time') if r else None)),
            (f'{col_prefix}_z_score', lambda r: _blank((r.scores or {}).get('z_score') if r else None)),
        ]
    key_prefix = f'{prefix}_{timing}'
    if scale.code == 'hamd':
        return [
            (f'{key_prefix}_total17', lambda r: _blank(r.total_score_17 if r else None)),
            (f'{key_prefix}_total21', lambda r: _blank(r.total_score_21 if r else None)),
            (f'{key_prefix}_improvement_rate17', lambda r: _blank(r.improvement_rate_17 if r else None)),
            (f'{key_prefix}_status', lambda r: _blank(r.status_label if r else None)),
        ]
    if scale.code == 'bacs':
        return [
            (f'{key_prefix}_{label}', (lambda r, f=field: _blank((r.scores or {}).get(f) if r else None)))
            for field, label in BACS_SUBFIELDS
        ]
    if scale.code == 'who-das':
        return [
            (f'{key_prefix}_{label}', (lambda r, f=field: _blank((r.scores or {}).get(f) if r else None)))
            for field, label in WHODAS_SUBFIELDS
        ]
    if scale.code == 'copm':
        return [
            (f'{key_prefix}_avg_performance', lambda r: _copm_avg(r, 'performance')),
            (f'{key_prefix}_avg_satisfaction', lambda r: _copm_avg(r, 'satisfaction')),
        ]
    if scale.code == '6mwt':
        return [(f'{key_prefix}_distance', lambda r: _blank(_six_mwt_distance(r)))]
    # Generic simple-total scale: phq9, sass-j, bdi-ii, sds, stai-*, dai-10,
    # and any future scale that stores a single total/score value.
    return [(f'{key_prefix}_total', lambda r: _blank(_generic_total(r)))]


def _build_scale_metadata():
    """[(scale, timing, [(col_key, extractor)])] in HAM-D -> BACS -> others order."""
    scales_by_code = {s.code: s for s in ScaleDefinition.objects.filter(is_active=True)}
    ordered_codes = ['hamd', 'bacs'] + sorted(
        code for code in scales_by_code if code not in ('hamd', 'bacs')
    )
    metadata = []
    for code in ordered_codes:
        scale = scales_by_code.get(code)
        if not scale:
            continue
        for timing in _timings_for_scale(scale):
            metadata.append((scale, timing, _timing_columns(scale, timing)))
    return metadata


def _last_treatment_date(patient, course_number):
    last_session = TreatmentSession.objects.filter(
        patient=patient, course_number=course_number
    ).order_by('-session_date', '-id').first()
    return last_session.session_date.isoformat() if last_session and last_session.session_date else ''


def _treatment_duration_days(patient, course_number, last_date_str=None):
    if not patient.first_treatment_date:
        return ''
    last_date_str = last_date_str if last_date_str is not None else _last_treatment_date(patient, course_number)
    if not last_date_str:
        return ''
    from datetime import date as _date
    last = _date.fromisoformat(last_date_str)
    delta = (last - patient.first_treatment_date).days
    return delta if delta >= 0 else ''


def _iter_export_scopes():
    """Yield explicit Course scopes, with a legacy fallback for pre-backfill patients."""
    courses = list(
        TreatmentCourse.objects.select_related('patient').order_by('patient__card_id', 'course_number')
    )
    for treatment_course in courses:
        yield treatment_course, treatment_course.patient, treatment_course.course_number
    for patient in Patient.objects.filter(treatment_courses__isnull=True).order_by('card_id'):
        yield None, patient, patient.course_number or 1


def generate_research_summary_csv():
    """1 row = 1 (card_id, course_number). Missing assessments are left blank."""
    scale_metadata = _build_scale_metadata()
    fieldnames = (
        ['card_id', 'course_number', 'age', 'gender', 'diagnosis', 'first_visit_date',
         'admission_date', 'first_treatment_date', 'discharge_date', 'status', 'weight_kg',
         'treatment_sessions_count', 'planned_sessions', 'last_treatment_date', 'treatment_duration_days']
        + [col_key for _, _, columns in scale_metadata for col_key, _ in columns]
        + ['ae_report_exists', 'ae_count', 'sae_count', 'sae_seizure', 'sae_finger_muscle',
           'sae_syncope', 'sae_mania', 'sae_suicide_attempt', 'sae_other']
    )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()

    gender_labels = dict(Patient.GENDER_CHOICES)
    status_labels = dict(Patient.STATUS_CHOICES)

    for treatment_course, patient, course_number in _iter_export_scopes():
        sessions = TreatmentSession.objects.filter(
            treatment_course=treatment_course,
        ).order_by('session_date', 'id') if treatment_course else TreatmentSession.objects.filter(
            patient=patient, course_number=course_number,
        ).order_by('session_date', 'id')
        last_session = sessions.last()
        last_treatment_date = last_session.session_date.isoformat() if last_session and last_session.session_date else ''
        row = {
            'card_id': patient.card_id,
            'course_number': course_number,
            'age': patient.age,
            'gender': gender_labels.get(patient.gender, ''),
            'diagnosis': patient.diagnosis,
            'first_visit_date': patient.first_visit_date.isoformat() if patient.first_visit_date else '',
            'admission_date': (
                treatment_course.admission_date
                if treatment_course and treatment_course.admission_date
                else patient.admission_date
            ).isoformat() if (
                treatment_course and treatment_course.admission_date
            ) or patient.admission_date else '',
            'first_treatment_date': (
                treatment_course.first_treatment_date
                if treatment_course and treatment_course.first_treatment_date
                else patient.first_treatment_date
            ).isoformat() if (
                treatment_course and treatment_course.first_treatment_date
            ) or patient.first_treatment_date else '',
            'discharge_date': (
                treatment_course.discharge_date
                if treatment_course and treatment_course.discharge_date
                else patient.discharge_date
            ).isoformat() if (
                treatment_course and treatment_course.discharge_date
            ) or patient.discharge_date else '',
            'status': status_labels.get(patient.status, ''),
            'weight_kg': str(patient.weight_kg) if patient.weight_kg is not None else '',
            'treatment_sessions_count': sessions.count(),
            'planned_sessions': MAX_PLANNED_SESSIONS,
            'last_treatment_date': last_treatment_date,
            'treatment_duration_days': _treatment_duration_days(patient, course_number, last_treatment_date),
        }

        for scale, timing, columns in scale_metadata:
            record = _get_record(patient, course_number, scale, timing)
            for col_key, extractor in columns:
                row[col_key] = extractor(record)

        sae_qs = SeriousAdverseEvent.objects.filter(
            session__treatment_course=treatment_course,
        ) if treatment_course else SeriousAdverseEvent.objects.filter(
            patient=patient, course_number=course_number,
        )
        sae_event_types = set()
        for event_types in sae_qs.values_list('event_types', flat=True):
            sae_event_types.update(event_types or [])
        row.update({
            'ae_report_exists': int((AdverseEventReport.objects.filter(
                session__treatment_course=treatment_course
            ) if treatment_course else AdverseEventReport.objects.filter(
                session__patient=patient, session__course_number=course_number
            )).exists()),
            'ae_count': (AdverseEventReport.objects.filter(
                session__treatment_course=treatment_course
            ) if treatment_course else AdverseEventReport.objects.filter(
                session__patient=patient, session__course_number=course_number
            )).count(),
            'sae_count': sae_qs.count(),
            'sae_seizure': int('seizure' in sae_event_types),
            'sae_finger_muscle': int('finger_muscle' in sae_event_types),
            'sae_syncope': int('syncope' in sae_event_types),
            'sae_mania': int('mania' in sae_event_types),
            'sae_suicide_attempt': int('suicide_attempt' in sae_event_types),
            'sae_other': int('other' in sae_event_types),
        })
        writer.writerow(row)

    return output.getvalue()


def generate_research_treatment_detail_csv():
    """1 row = 1 TreatmentSession, across all patients."""
    side_effect_keys = [(item['key'], item['label']) for item in SIDE_EFFECT_ITEMS]
    fieldnames = [
        'card_id', 'course_number', 'session_no', 'session_date', 'status',
        'coil_type', 'target_site',
        'mt_percent', 'intensity_percent', 'frequency_hz', 'train_seconds',
        'intertrain_seconds', 'train_count', 'total_pulses', 'treatment_notes',
    ]
    for key, _label in side_effect_keys:
        fieldnames += [
            f'sideeffect_{key}_before', f'sideeffect_{key}_during',
            f'sideeffect_{key}_after', f'sideeffect_{key}_relatedness',
            f'sideeffect_{key}_memo',
        ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()

    status_labels = dict(TreatmentSession.STATUS_CHOICES)

    for treatment_course, patient, course_number in _iter_export_scopes():
        sessions = list((TreatmentSession.objects.filter(
            treatment_course=treatment_course,
        ) if treatment_course else TreatmentSession.objects.filter(
            patient=patient, course_number=course_number,
        )).order_by('session_date', 'id'))
        number_map = {session.id: number for number, session in enumerate(
            sessions[:MAX_PLANNED_SESSIONS], start=1
        )}
        side_effects_by_session = {
            se.session_id: se.rows or []
            for se in SideEffectCheck.objects.filter(session__in=sessions)
        }

        for session in sessions:
            row = {
                'card_id': patient.card_id,
                'course_number': course_number,
                'session_no': number_map.get(session.id, ''),
                'session_date': session.session_date.isoformat() if session.session_date else '',
                'status': status_labels.get(session.status, ''),
                'coil_type': session.coil_type,
                'target_site': session.target_site,
                'mt_percent': _blank(session.mt_percent),
                'intensity_percent': _blank(session.intensity_percent),
                'frequency_hz': _blank(session.frequency_hz),
                'train_seconds': _blank(session.train_seconds),
                'intertrain_seconds': _blank(session.intertrain_seconds),
                'train_count': _blank(session.train_count),
                'total_pulses': _blank(session.total_pulses),
                'treatment_notes': session.treatment_notes,
            }

            entries_by_key = {}
            for entry in side_effects_by_session.get(session.id, []):
                key = SIDE_EFFECT_LABEL_ALIASES.get(entry.get('item'))
                if key:
                    entries_by_key[key] = entry
            for key, _label in side_effect_keys:
                entry = entries_by_key.get(key, {})
                row[f'sideeffect_{key}_before'] = _blank(entry.get('before'))
                row[f'sideeffect_{key}_during'] = _blank(entry.get('during'))
                row[f'sideeffect_{key}_after'] = _blank(entry.get('after'))
                row[f'sideeffect_{key}_relatedness'] = _blank(entry.get('relatedness'))
                row[f'sideeffect_{key}_memo'] = entry.get('memo', '') or ''

            writer.writerow(row)

    return output.getvalue()


def generate_research_adverse_events_csv():
    """1 row = 1 SeriousAdverseEvent, joined with AdverseEventReport when present."""
    fieldnames = [
        'card_id', 'course_number', 'session_no',
        'event_types', 'onset_date',
        'adverse_event_name', 'diagnosis_category', 'age', 'sex', 'initials',
        'rmt_value', 'intensity_value', 'stimulation_site',
        'treatment_course_number',
        'outcome', 'notes',
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()

    site_labels = dict(AdverseEventReport.SITE_CHOICES)
    diagnosis_labels = dict(AdverseEventReport.DIAGNOSIS_CHOICES)
    outcome_labels = dict(AdverseEventReport.OUTCOME_CHOICES)

    for treatment_course, patient, course_number in _iter_export_scopes():
        course_sessions = list((TreatmentSession.objects.filter(
            treatment_course=treatment_course,
        ) if treatment_course else TreatmentSession.objects.filter(
            patient=patient, course_number=course_number,
        )).order_by('session_date', 'id'))
        number_map = {session.id: number for number, session in enumerate(
            course_sessions[:MAX_PLANNED_SESSIONS], start=1
        )}
        saes = (SeriousAdverseEvent.objects.filter(
            session__treatment_course=treatment_course,
        ) if treatment_course else SeriousAdverseEvent.objects.filter(
            patient=patient, course_number=course_number,
        )).select_related('session').order_by('created_at')

        for sae in saes:
            report = getattr(sae.session, 'adverse_event_report', None)
            site = ''
            if report:
                site = site_labels.get(report.stimulation_site_category, '')
                if report.stimulation_site_category == 'other' and report.stimulation_site_other_text:
                    site = report.stimulation_site_other_text
            writer.writerow({
                'card_id': patient.card_id,
                'course_number': course_number,
                'session_no': number_map.get(sae.session_id, ''),
                'event_types': ','.join(sae.event_types or []),
                'onset_date': report.onset_date.isoformat() if report and report.onset_date else '',
                'adverse_event_name': report.adverse_event_name if report else '',
                'diagnosis_category': diagnosis_labels.get(report.diagnosis_category, '') if report else '',
                'age': _blank(report.age) if report else '',
                'sex': report.sex if report else '',
                'initials': report.initials if report else '',
                'rmt_value': _blank(report.rmt_value) if report else '',
                'intensity_value': _blank(report.intensity_value) if report else '',
                'stimulation_site': site,
                'treatment_course_number': _blank(report.treatment_course_number) if report else '',
                'outcome': ','.join(
                    outcome_labels.get(flag, flag) for flag in (report.outcome_flags or [])
                ) if report else '',
                'notes': report.special_notes if report else '',
            })

    return output.getvalue()
