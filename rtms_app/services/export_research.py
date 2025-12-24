"""
Research CSV export service.

Handles horizontal (wide format) CSV generation for research purposes,
with flexible column selection by category.
"""

import csv
import io
from decimal import Decimal
from django.utils import timezone
from django.db.models import Q, Count, Max, Min
from rtms_app.models import Patient, TreatmentSession, AssessmentRecord, SeriousAdverseEvent, AdverseEventReport


class ResearchCSVExporter:
    """
    Generates research-friendly CSV in wide format.
    One row = (card_id, course_number) with all patient/treatment/assessment data horizontally.
    """
    
    # Column definitions by category
    CATEGORIES = {
        'patient_basic': {
            'label': '患者基本属性',
            'columns': [
                ('card_id', 'カルテ番号', lambda p, *args: p.card_id),
                ('course_number', 'クール数', lambda p, *args: p.course_number),
                ('name', '氏名', lambda p, *args: p.name),
                ('birth_date', '生年月日', lambda p, *args: p.birth_date.isoformat() if p.birth_date else ''),
                ('age', '年齢', lambda p, *args: p.age),
                ('gender', '性別', lambda p, *args: dict(p.GENDER_CHOICES).get(p.gender, '')),
                ('protocol_type', 'プロトコル', lambda p, *args: dict(p.PROTOCOL_CHOICES).get(p.protocol_type, '')),
                ('diagnosis', '診断名', lambda p, *args: p.diagnosis),
                ('status', '患者ステータス', lambda p, *args: dict(p.STATUS_CHOICES).get(p.status, '')),
            ]
        },
        'treatment_summary': {
            'label': '治療サマリ',
            'columns': [
                ('first_treatment_date', '初回治療日', lambda p, *args: p.first_treatment_date.isoformat() if p.first_treatment_date else ''),
                ('mapping_date', '初回位置決め日', lambda p, *args: p.mapping_date.isoformat() if p.mapping_date else ''),
                ('admission_date', '入院予定日', lambda p, *args: p.admission_date.isoformat() if p.admission_date else ''),
                ('discharge_date', '退院日', lambda p, *args: p.discharge_date.isoformat() if p.discharge_date else ''),
                ('treatment_sessions_count', '実施治療回数', lambda p, *args: _count_treatment_sessions(p, *args)),
                ('planned_sessions', '予定治療回数', lambda p, *args: _get_planned_sessions(p)),
                ('last_treatment_date', '最終治療日', lambda p, *args: _get_last_treatment_date(p, *args)),
                ('treatment_duration_days', '治療期間（日）', lambda p, *args: _get_treatment_duration(p, *args)),
            ]
        },
        'hamd': {
            'label': 'HAM-D評価',
            'columns': [
                ('hamd_baseline_17', 'HAM-D17（ベースライン）', lambda p, *args: _get_hamd_by_timing(p, 'baseline', 17)),
                ('hamd_baseline_21', 'HAM-D21（ベースライン）', lambda p, *args: _get_hamd_by_timing(p, 'baseline', 21)),
                ('hamd_3w_17', 'HAM-D17（第3週）', lambda p, *args: _get_hamd_by_timing(p, '3w', 17)),
                ('hamd_3w_21', 'HAM-D21（第3週）', lambda p, *args: _get_hamd_by_timing(p, '3w', 21)),
                ('hamd_3w_improvement', 'HAM-D17改善率（第3週）', lambda p, *args: _format_percent(_get_hamd_improvement(p, '3w', 17))),
                ('hamd_3w_status', '判定（第3週）', lambda p, *args: _get_hamd_status(p, '3w')),
                ('hamd_4w_17', 'HAM-D17（第4週）', lambda p, *args: _get_hamd_by_timing(p, '4w', 17)),
                ('hamd_4w_21', 'HAM-D21（第4週）', lambda p, *args: _get_hamd_by_timing(p, '4w', 21)),
                ('hamd_4w_improvement', 'HAM-D17改善率（第4週）', lambda p, *args: _format_percent(_get_hamd_improvement(p, '4w', 17))),
                ('hamd_4w_status', '判定（第4週）', lambda p, *args: _get_hamd_status(p, '4w')),
                ('hamd_6w_17', 'HAM-D17（第6週）', lambda p, *args: _get_hamd_by_timing(p, '6w', 17)),
                ('hamd_6w_21', 'HAM-D21（第6週）', lambda p, *args: _get_hamd_by_timing(p, '6w', 21)),
                ('hamd_6w_improvement', 'HAM-D17改善率（第6週）', lambda p, *args: _format_percent(_get_hamd_improvement(p, '6w', 17))),
                ('hamd_6w_status', '判定（第6週）', lambda p, *args: _get_hamd_status(p, '6w')),
            ]
        },
        'adverse_events': {
            'label': '有害事象',
            'columns': [
                ('ae_report_exists', '有害事象報告書作成', lambda p, *args: '有' if _has_adverse_event_report(p, *args) else '無'),
                ('ae_count', '有害事象数', lambda p, *args: _count_adverse_events(p, *args)),
                ('sae_count', '重篤有害事象数', lambda p, *args: _count_serious_adverse_events(p, *args)),
                ('sae_seizure', '重篤-けいれん発作', lambda p, *args: '有' if _has_sae_event(p, 'seizure', *args) else '無'),
                ('sae_finger_muscle', '重篤-手指筋収縮', lambda p, *args: '有' if _has_sae_event(p, 'finger_muscle', *args) else '無'),
                ('sae_syncope', '重篤-失神', lambda p, *args: '有' if _has_sae_event(p, 'syncope', *args) else '無'),
                ('sae_mania', '重篤-躁病/軽躁病出現', lambda p, *args: '有' if _has_sae_event(p, 'mania', *args) else '無'),
                ('sae_suicide', '重篤-自殺企図', lambda p, *args: '有' if _has_sae_event(p, 'suicide_attempt', *args) else '無'),
                ('sae_other', '重篤-その他', lambda p, *args: '有' if _has_sae_event(p, 'other', *args) else '無'),
            ]
        },
    }

    def __init__(self, selected_categories=None):
        """
        Initialize with selected categories.
        selected_categories: list of category keys to include (default: all)
        """
        self.selected_categories = selected_categories or list(self.CATEGORIES.keys())
        self.columns = self._build_columns()

    def _build_columns(self):
        """Build flat list of (key, label, getter) from selected categories."""
        columns = []
        for cat_key in self.selected_categories:
            if cat_key in self.CATEGORIES:
                columns.extend(self.CATEGORIES[cat_key]['columns'])
        return columns

    def get_category_choices(self):
        """Return list of (key, label) for UI checkboxes."""
        return [(key, cat['label']) for key, cat in self.CATEGORIES.items()]

    def generate_csv(self, patients_data):
        """
        Generate CSV content for given patients.
        
        patients_data: list of (Patient, related_sessions) tuples
        Returns: CSV string (UTF-8-SIG)
        """
        output = io.StringIO()
        
        # Write headers
        headers = [label for _, label, _ in self.columns]
        writer = csv.DictWriter(output, fieldnames=[col[0] for col in self.columns], extrasaction='ignore')
        writer.writerow({col[0]: col[1] for col in self.columns})
        
        # Write data rows
        for patient, related_data in patients_data:
            row = {}
            for key, label, getter in self.columns:
                try:
                    value = getter(patient, related_data)
                    row[key] = value if value is not None else ''
                except Exception as e:
                    row[key] = f'ERROR: {str(e)}'
            writer.writerow(row)
        
        # Return as UTF-8-SIG (Excel-friendly)
        return output.getvalue().encode('utf-8-sig').decode('utf-8-sig')


# Helper functions for getters

def _count_treatment_sessions(patient, related_data=None):
    """Count treatment sessions for patient and course."""
    return TreatmentSession.objects.filter(
        patient=patient,
        course_number=patient.course_number
    ).count()


def _get_planned_sessions(patient):
    """Get planned total sessions (default 30, customize by protocol later)."""
    # For now, default to 30. Can be customized by protocol_type later.
    return 30


def _get_last_treatment_date(patient, related_data=None):
    """Get last treatment date for patient and course."""
    last_session = TreatmentSession.objects.filter(
        patient=patient,
        course_number=patient.course_number
    ).order_by('-date').first()
    return last_session.date.date().isoformat() if last_session else ''


def _get_treatment_duration(patient, related_data=None):
    """Get number of days from first to last treatment."""
    if not patient.first_treatment_date:
        return ''
    last_date = _get_last_treatment_date(patient, related_data)
    if not last_date:
        return ''
    from datetime import datetime
    last = datetime.fromisoformat(last_date).date()
    delta = (last - patient.first_treatment_date).days
    return str(delta) if delta >= 0 else ''


def _get_hamd_by_timing(patient, timing, version=17):
    """Get HAM-D score for given timing and version (17 or 21)."""
    record = AssessmentRecord.objects.filter(
        patient=patient,
        course_number=patient.course_number,
        timing=timing,
        scale__code='hamd'
    ).first()
    if not record:
        return ''
    return record.total_score_17 if version == 17 else record.total_score_21


def _get_hamd_improvement(patient, timing, version=17):
    """Get HAM-D improvement rate (%) for given timing."""
    record = AssessmentRecord.objects.filter(
        patient=patient,
        course_number=patient.course_number,
        timing=timing,
        scale__code='hamd'
    ).first()
    return record.improvement_rate_17 if record and version == 17 else None


def _format_percent(value):
    """Format percentage value."""
    if value is None:
        return ''
    return f'{value:.1f}%'


def _get_hamd_status(patient, timing):
    """Get HAM-D status label for given timing."""
    record = AssessmentRecord.objects.filter(
        patient=patient,
        course_number=patient.course_number,
        timing=timing,
        scale__code='hamd'
    ).first()
    return record.status_label if record else ''


def _has_adverse_event_report(patient, related_data=None):
    """Check if patient has any adverse event reports."""
    return AdverseEventReport.objects.filter(
        patient=patient,
        course_number=patient.course_number
    ).exists()


def _count_adverse_events(patient, related_data=None):
    """Count adverse event reports."""
    return AdverseEventReport.objects.filter(
        patient=patient,
        course_number=patient.course_number
    ).count()


def _count_serious_adverse_events(patient, related_data=None):
    """Count serious adverse events."""
    return SeriousAdverseEvent.objects.filter(
        patient=patient,
        course_number=patient.course_number
    ).count()


def _has_sae_event(patient, event_type, related_data=None):
    """Check if patient has specific SAE event type."""
    return SeriousAdverseEvent.objects.filter(
        patient=patient,
        course_number=patient.course_number,
        event_types__contains=event_type
    ).exists()
