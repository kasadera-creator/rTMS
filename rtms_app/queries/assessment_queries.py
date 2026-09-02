"""
評価（Assessment）関連のクエリ最適化層
Stage 9: Assessment query/data-access layer 重複整理
Phase 10a: AssessmentRecord prioritization pattern (print views migration)
Phase 10b: Assessment write path consolidation
"""
from django.db.models import QuerySet
from rtms_app.models import Assessment, Patient, TreatmentCourse, AssessmentRecord, ScaleDefinition
from typing import Optional, Union, List, Dict, Any, Tuple


def resolve_legacy_treatment_course(patient: Patient, course_number: int = None):
    course_number = course_number or patient.course_number or 1
    return TreatmentCourse.objects.filter(
        patient=patient, course_number=course_number,
    ).first()


def validate_treatment_course_scope(patient: Patient, course_number: int, treatment_course: TreatmentCourse):
    if treatment_course is None:
        raise ValueError("TreatmentCourse is required for a normal Course write")
    if treatment_course.patient_id != patient.id:
        raise ValueError("TreatmentCourse belongs to a different patient")
    if course_number is not None and treatment_course.course_number != course_number:
        raise ValueError("TreatmentCourse and course_number do not match")
    return treatment_course


def require_treatment_course(patient: Patient, course_number: int, treatment_course: TreatmentCourse):
    """Validate the explicit Course required by a normal write path."""
    return validate_treatment_course_scope(patient, course_number, treatment_course)


def resolve_treatment_course(patient: Patient, course_number: int = None, treatment_course=None):
    if treatment_course is not None:
        return validate_treatment_course_scope(patient, course_number, treatment_course)
    return resolve_legacy_treatment_course(patient, course_number)
def get_assessments_ordered(patient: Patient) -> QuerySet[Assessment]:
    """
    患者の全Assessmentを日付順に取得

    用途：
    - 評価履歴の表示
    - 最新評価抽出の基底データセット

    Args:
        patient: Patient インスタンス

    Returns:
        QuerySet[Assessment]: 日付昇順のAssessmentクエリセット

    Example:
        >>> history = get_assessments_ordered(patient)
        >>> for a in history:
        ...     print(a.date, a.timing, a.total_score_17)
    """
    return Assessment.objects.filter(patient=patient).order_by('date')


def get_latest_assessment(patient: Patient, timing: str) -> Optional[Assessment]:
    """
    患者の指定タイミングでの最新評価を取得

    用途：
    - baseline/week3/week4/week6 の最新評価検索
    - 改善率計算の基準値取得
    - 臨床経過表示

    Args:
        patient: Patient インスタンス
        timing: 評価タイミング (baseline, week3, week4, week6, other)

    Returns:
        Optional[Assessment]: 最新のAssessmentインスタンス、該当なしの場合はNone

    Example:
        >>> baseline = get_latest_assessment(patient, 'baseline')
        >>> if baseline:
        ...     print(f"Baseline score: {baseline.total_score_17}")
        >>> week3 = get_latest_assessment(patient, 'week3')
    """
    return Assessment.objects.filter(patient=patient, timing=timing).order_by('-date').first()


def get_assessment_by_timing_with_fallback(
    patient: Patient,
    timing: str,
    scale: ScaleDefinition,
    course_number: int = None
) -> Optional[Union[AssessmentRecord, Assessment]]:
    """
    患者の指定タイミングでの評価を取得（新旧モデルのfallback対応）

    用途：
    - AssessmentRecord（新モデル）を優先して検索
    - AssessmentRecord が存在しない場合は Assessment（旧モデル）をfallback
    - 新旧モデルの混在移行期に対応

    Args:
        patient: Patient インスタンス
        timing: 評価タイミング (baseline, week3, week4, week6, other)
        scale: ScaleDefinition インスタンス (scale.code で検索対象判定)
        course_number: コース番号 (デフォルト: patient.course_number or 1)

    Returns:
        Optional[Union[AssessmentRecord, Assessment]]:
        - AssessmentRecord が存在 → AssessmentRecord を返す
        - AssessmentRecord がない、Assessment が存在 → Assessment を返す（HAM-D のみ）
        - 両方存在しない → None

    Fallback Logic:
        1. AssessmentRecord.objects.filter(patient, course_number, timing, scale)
           .order_by('-date').first() を検索
        2. None の場合、scale.code == 'hamd' なら
           Assessment.objects.filter(patient, course_number, timing, type='HAM-D')
           .order_by('-date').first() をfallback
        3. 両方 None なら None を返す

    Example:
        >>> scale = ScaleDefinition.objects.get(code='hamd')
        >>> baseline = get_assessment_by_timing_with_fallback(patient, 'baseline', scale)
        >>> if baseline:
        ...     print(f"Score: {baseline.total_score_17}")
    """
    if course_number is None:
        course_number = patient.course_number or 1

    # Try new model first (AssessmentRecord)
    record = AssessmentRecord.objects.filter(
        patient=patient,
        course_number=course_number,
        timing=timing,
        scale=scale,
    ).order_by('-date').first()

    if record is not None:
        return record

    # Fallback to legacy model (Assessment) only for HAM-D
    if scale.code == 'hamd':
        legacy = Assessment.objects.filter(
            patient=patient,
            course_number=course_number,
            timing=timing,
            type='HAM-D',
        ).order_by('-date').first()
        return legacy

    return None


def get_baseline_assessments_ordered(patient: Patient) -> List[Union[AssessmentRecord, Assessment]]:
    """
    患者のbaseline評価を全て取得（新旧モデルのfallback対応）

    Phase 10a: print_views.py の baseline query 統一用ヘルパー

    用途：
    - 印刷ビュー（admission, suitability等）での baseline 評価一覧取得
    - AssessmentRecord（新モデル）を優先、なければ Assessment（旧モデル）をfallback
    - 日付昇順で返却

    Args:
        patient: Patient インスタンス

    Returns:
        List[Union[AssessmentRecord, Assessment]]: baseline評価を日付昇順で返す
        - AssessmentRecordが存在 → AssessmentRecordのリストを返す
        - AssessmentRecordがない＆Assessmentが存在 → Assessmentのリストを返す
        - 両方ない → 空リスト []

    Fallback Logic:
        1. AssessmentRecord（scale='hamd', timing='baseline'）を検索
        2. レコードが存在 → その結果セットを日付昇順で返す
        3. レコードがない → Assessment（type='HAM-D', timing='baseline'）をfallback
        4. 両方ない → 空リスト

    Example:
        >>> assessments = get_baseline_assessments_ordered(patient)
        >>> for a in assessments:
        ...     print(a.date, a.total_score_17)
    """
    try:
        hamd_scale = ScaleDefinition.objects.get(code='hamd')
    except ScaleDefinition.DoesNotExist:
        # hamd scale が存在しない場合は Assessment をfallback
        return list(Assessment.objects.filter(
            patient=patient, timing='baseline', type='HAM-D'
        ).order_by('date'))

    # Try new model first (AssessmentRecord)
    new_records = list(AssessmentRecord.objects.filter(
        patient=patient,
        timing='baseline',
        scale=hamd_scale,
    ).order_by('date'))

    if new_records:
        return new_records

    # Fallback to legacy model (Assessment)
    return list(Assessment.objects.filter(
        patient=patient,
        timing='baseline',
        type='HAM-D',
    ).order_by('date'))


# ============================================================================
# Phase 10b: Write Path Consolidation
# ============================================================================

def save_assessment_record(
    patient: Patient,
    course_number: int,
    timing: str,
    scale: ScaleDefinition,
    date: Any,
    scores: Dict[str, Any],
    note: str = "",
    defaults_override: Optional[Dict[str, Any]] = None
) -> Tuple[AssessmentRecord, bool]:
    """
    AssessmentRecord を作成・更新（共通ロジック）

    Phase 10b: 複数の write path から呼び出される共通 helper

    用途：
    - assessment_scale_form() からの AssessmentRecord write
    - 他の entry point からの汎用 AssessmentRecord write

    Args:
        patient: Patient インスタンス
        course_number: コース番号
        timing: 評価タイミング (baseline, week3, week4, week6, other, post)
        scale: ScaleDefinition インスタンス
        date: 評価日（datetime.date）
        scores: スコア辞書（{"q1": "0", ...}）
        note: 特記事項（デフォルト: ""）
        defaults_override: デフォルト値のオーバーライド（デフォルト: None）

    Returns:
        Tuple[AssessmentRecord, bool]: (作成/更新されたレコード, 作成フラグ)

    Note:
        - Model.save() により calculate_scores() は自動的に呼ばれる
        - 明示的な calculate_scores() 呼び出しは不要

    Example:
        >>> scale = ScaleDefinition.objects.get(code='hamd')
        >>> record, created = save_assessment_record(
        ...     patient=patient,
        ...     course_number=1,
        ...     timing='week3',
        ...     scale=scale,
        ...     date=datetime.date.today(),
        ...     scores={'q1': '1', 'q2': '2', ...},
        ...     note="改善あり"
        ... )
    """
    defaults = {
        'date': date,
        'scores': scores,
        'note': note,
    }
    if defaults_override:
        defaults.update(defaults_override)

    record, created = AssessmentRecord.objects.update_or_create(
        patient=patient,
        course_number=course_number,
        timing=timing,
        scale=scale,
        defaults=defaults,
    )
    # Model.save() will automatically call calculate_scores()
    return record, created


def save_assessment_hamd(
    patient: Patient,
    course_number: int,
    timing: str,
    date: Any,
    scores: Dict[str, Any],
    note: str = ""
) -> Tuple[Assessment, bool]:
    """
    Assessment (HAM-D) を作成・更新（共通ロジック）

    Phase 10b: 複数の write path から呼び出される共通 helper

    用途：
    - assessment_scale_form() からの legacy Assessment (HAM-D) write
    - assessment_add_legacy() からの Assessment write
    - 新旧モデルの同期保持

    Args:
        patient: Patient インスタンス
        course_number: コース番号
        timing: 評価タイミング
        date: 評価日（datetime.date）
        scores: スコア辞書（{"q1": "0", ...}）
        note: 特記事項（デフォルト: ""）

    Returns:
        Tuple[Assessment, bool]: (作成/更新されたレコード, 作成フラグ)

    Note:
        - Unique constraint: (patient, course_number, timing, type='HAM-D')
        - Model.save() により calculate_scores() は type=='HAM-D' の場合自動的に呼ばれる
        - 明示的な calculate_scores() 呼び出しは不要

    Example:
        >>> legacy, created = save_assessment_hamd(
        ...     patient=patient,
        ...     course_number=1,
        ...     timing='baseline',
        ...     date=datetime.date.today(),
        ...     scores={'q1': '0', 'q2': '1', ...},
        ...     note=""
        ... )
    """
    defaults = {
        'date': date,
        'scores': scores,
        'note': note,
        'type': 'HAM-D',
    }
    legacy, created = Assessment.objects.update_or_create(
        patient=patient,
        course_number=course_number,
        timing=timing,
        type='HAM-D',
        defaults=defaults,
    )
    # Model.save() will automatically call calculate_scores() if type == 'HAM-D'
    return legacy, created
