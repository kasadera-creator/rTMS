from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any

from django.utils import timezone

from ..models import Assessment, Patient, ScaleDefinition
from ..queries.assessment_queries import get_assessment_by_timing_with_fallback, resolve_treatment_course


@dataclass
class Recommendation:
    status: str  # remission/effective/ineffective/pending
    message: str
    detail: str
    scale: str
    baseline: Optional[int]
    current: Optional[int]
    improvement_rate: Optional[float]
    updated_at: Optional[str]

    def to_context(self) -> Dict[str, Any]:
        return {
            'status': self.status,
            'message': self.message,
            'detail': self.detail,
            'scale': self.scale,
            'baseline': self.baseline,
            'current': self.current,
            'improvement_rate': self.improvement_rate,
            'updated_at': self.updated_at,
        }


def _choose_scale(score17: int, score21: int) -> tuple[str, int]:
    """Prefer HAMD17 when available; otherwise use HAMD21."""
    if score17 and score17 > 0:
        return ('HAMD17', score17)
    if score21 and score21 > 0:
        return ('HAMD21', score21)
    return ('HAMD17', 0)


def get_patient_recommendation(
    patient: Patient, treatment_course=None, course_number: int = None,
) -> Recommendation:
    """
    Compute week-3 recommendation based on HAMD.
    Rules:
    - Remission if HAMD17 <= 7 or HAMD21/24 equivalent <= 9
    - If not remission and improvement < 20% vs baseline -> ineffective
    - Else effective (continue to 30 sessions)
    """
    now = timezone.localtime(timezone.now())

    treatment_course = resolve_treatment_course(
        patient, course_number=course_number, treatment_course=treatment_course,
    )
    scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number or patient.course_number or 1,
    }
    hamd_scale = ScaleDefinition.objects.filter(code='hamd').first()
    if hamd_scale is not None:
        baseline = get_assessment_by_timing_with_fallback(
            patient, 'baseline', hamd_scale, course_number=course_number,
            treatment_course=treatment_course,
        )
        week3 = get_assessment_by_timing_with_fallback(
            patient, 'week3', hamd_scale, course_number=course_number,
            treatment_course=treatment_course,
        )
    else:
        baseline = Assessment.objects.filter(**scope, timing='baseline').order_by('-date').first()
        week3 = Assessment.objects.filter(**scope, timing='week3').order_by('-date').first()

    if not week3 or not (week3.total_score_17 or week3.total_score_21):
        return Recommendation(
            status='pending',
            message='第3週目のHAMD評価が未入力です',
            detail='評価入力後に推奨プロトコルを表示します',
            scale='-', baseline=None, current=None, improvement_rate=None,
            updated_at=None,
        )

    scale, current = _choose_scale(week3.total_score_17, week3.total_score_21)

    # Remission thresholds
    is_remission = (week3.total_score_17 and week3.total_score_17 <= 7) or (week3.total_score_21 and week3.total_score_21 <= 9)
    if is_remission:
        msg = '寛解：漸減プロトコルへ移行'
        detail = '第4週 週3回・第5週 週2回・第6週 週1回まで（中止または漸減）'
        return Recommendation('remission', msg, detail, scale, baseline.total_score_17 if baseline else None, current, None, week3.date.strftime('%Y-%m-%d'))

    # Effectiveness vs baseline (>=20% improvement)
    baseline_score = 0
    if baseline:
        b_scale, b_value = _choose_scale(baseline.total_score_17, baseline.total_score_21)
        baseline_score = b_value

    if baseline_score > 0:
        improvement = (baseline_score - current) / baseline_score
    else:
        improvement = None

    if improvement is not None and improvement < 0.20:
        msg = '治療無効：中止を検討（続行可）'
        detail = f'{scale} 改善率 {int(improvement*100)}%（20%未満）→ 中止検討'
        return Recommendation('ineffective', msg, detail, scale, baseline_score or None, current, improvement, week3.date.strftime('%Y-%m-%d'))

    msg = '有効性あり：治療継続（合計30回）'
    detail = f'{scale} 改善率 {int(improvement*100)}%（20%以上）'
    return Recommendation('effective', msg, detail, scale, baseline_score or None, current, improvement, week3.date.strftime('%Y-%m-%d'))
