"""
評価（Assessment）関連のクエリ最適化層
Stage 9: Assessment query/data-access layer 重複整理
Phase 10a: AssessmentRecord prioritization pattern (print views migration)
"""
from django.db.models import QuerySet
from rtms_app.models import Assessment, Patient, AssessmentRecord, ScaleDefinition
from typing import Optional, Union, List


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
