from typing import List, Dict, Any, Optional
from django.utils import timezone
from rtms_app.models import TreatmentSession, Assessment, AssessmentRecord


def build_treatment_session_display(patient, course_number: int = 1) -> List[Dict[str, Any]]:
    """Return a list of display dicts for treatment sessions for the patient/course.

    Each dict contains keys used by templates: count, date, mt, stim_pct_mt,
    output_pct, frequency_hz, train_seconds, intertrain_seconds, train_count, se
    """
    sessions = (
        TreatmentSession.objects.filter(patient=patient, course_number=course_number)
        .order_by('session_date', 'date')
    )
    out = []
    for i, s in enumerate(sessions, 1):
        # MT: prefer mt_percent then motor_threshold
        mt = getattr(s, 'mt_percent', None) if getattr(s, 'mt_percent', None) is not None else getattr(s, 'motor_threshold', None)
        # stim percent: prefer intensity_percent then intensity
        stim_pct = getattr(s, 'intensity_percent', None) if getattr(s, 'intensity_percent', None) is not None else getattr(s, 'intensity', None)

        # compute output percent when possible
        try:
            if mt is not None and stim_pct is not None:
                output = round(float(mt) * (float(stim_pct) / 100.0))
            else:
                output = None
        except Exception:
            output = None

        freq = getattr(s, 'frequency_hz', None) or 18
        train_seconds = getattr(s, 'train_seconds', None) or 2
        intertrain_seconds = getattr(s, 'intertrain_seconds', None) or 20
        train_count = getattr(s, 'train_count', None) or 55

        # side effect summary
        se = None
        try:
            se_map = {
                'headache': '頭痛', 'scalp': '頭皮痛', 'discomfort': '不快感',
                'tooth': '歯痛', 'twitch': '攣縮', 'dizzy': 'めまい', 'nausea': '吐き気',
                'tinnitus': '耳鳴り', 'hearing': '聴力低下', 'anxiety': '不安', 'other': 'その他'
            }
            parts = []
            if s.side_effects:
                for k, v in (s.side_effects or {}).items():
                    if k != 'note' and v and str(v) != '0':
                        parts.append(se_map.get(k, k))
            se = '、'.join(parts) if parts else 'なし'
        except Exception:
            se = 'なし'

        out.append({
            'count': i,
            'date': getattr(s, 'session_date', None) or getattr(s, 'date', None),
            'mt': mt,
            'stim_pct_mt': stim_pct,
            'output_pct': output,
            'frequency_hz': freq,
            'train_seconds': train_seconds,
            'intertrain_seconds': intertrain_seconds,
            'train_count': train_count,
            'se': se,
        })
    return out


def build_assessment_trend(patient, timings: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Build trend columns for HAMD display. Returns list of dicts (label,date_str,hamd21,hamd17,improvement_pct_17,status_label)

    Default timings are baseline, week3, week4, week6 (same as UI elsewhere).
    Pulls from both new AssessmentRecord and legacy Assessment, preferring AssessmentRecord if both exist.
    """
    if timings is None:
        timings = ['baseline', 'week3', 'week4', 'week6']

    # Fetch from both models to ensure coverage
    assessment_records = AssessmentRecord.objects.filter(patient=patient).order_by('-date')
    legacy_assessments = Assessment.objects.filter(patient=patient).order_by('-date')

    # For each timing, prefer AssessmentRecord, fall back to legacy Assessment
    latest_by_timing = {}
    for t in timings:
        a = assessment_records.filter(timing=t).order_by('-date').first()
        if a is None:
            a = legacy_assessments.filter(timing=t).order_by('-date').first()
        latest_by_timing[t] = a

    # find baseline for improvement calculations
    baseline_obj = latest_by_timing.get('baseline')
    baseline_17 = getattr(baseline_obj, 'total_score_17', None)

    cols = []
    from django.utils.dateformat import format as dateformat
    for t in timings:
        a = latest_by_timing.get(t)
        date_str = a.date.strftime('%Y/%-m/%-d') if a and getattr(a, 'date', None) else '-'
        hamd17 = getattr(a, 'total_score_17', None)
        hamd21 = getattr(a, 'total_score_21', None)
        improvement_pct_17 = None
        if t != 'baseline' and a and baseline_17 not in (None, 0) and hamd17 is not None:
            try:
                improvement_pct_17 = round((baseline_17 - hamd17) / baseline_17 * 100.0, 1)
            except Exception:
                improvement_pct_17 = None

        # label resolution: prefer model display, else friendly mapping
        if a and hasattr(a, 'get_timing_display'):
            label = a.get_timing_display()
        else:
            # map known timing codes to labels
            LABELS = {'baseline': '治療前', 'week3': '3週', 'week4': '4週', 'week6': '6週'}
            label = LABELS.get(t, t)

        status_label = getattr(a, 'status_label', '') if a and hasattr(a, 'status_label') else ''

        cols.append({
            'timing': t,
            'label': label,
            'date_str': date_str,
            'hamd21': hamd21,
            'hamd17': hamd17,
            'improvement_pct_17': improvement_pct_17,
            'status_label': status_label,
        })
    return cols
