"""
評価（Assessment）関連のクエリ最適化層
Stage 9: Assessment query/data-access layer 重複整理
"""
from django.db.models import QuerySet
from rtms_app.models import Assessment, Patient
from typing import Optional


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
