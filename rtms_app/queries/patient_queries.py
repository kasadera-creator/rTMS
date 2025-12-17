"""
患者関連のクエリ最適化層
select_related/prefetch_relatedの集約
"""
from django.db.models import QuerySet, Count, Q, Prefetch
from rtms_app.models import Patient, Assessment, TreatmentSession
from typing import Optional
from datetime import date


def get_patients_for_dashboard(target_date: date) -> QuerySet[Patient]:
    """
    ダッシュボード用の患者一覧
    必要な関連データを効率的に取得
    """
    return Patient.objects.select_related(
        'attending_physician'
    ).prefetch_related(
        Prefetch(
            'treatmentsession_set',
            queryset=TreatmentSession.objects.filter(
                date__date=target_date
            )
        ),
        Prefetch(
            'assessment_set',
            queryset=Assessment.objects.filter(
                date=target_date
            )
        )
    ).order_by('card_id')


def get_patient_with_sessions(patient_id: int) -> Patient:
    """
    治療セッション付きの患者データ取得
    """
    return Patient.objects.select_related(
        'attending_physician'
    ).prefetch_related(
        'treatmentsession_set',
        'assessment_set'
    ).get(pk=patient_id)


def get_patients_needing_baseline(target_date: date) -> QuerySet[Patient]:
    """
    ベースライン評価が必要な患者
    """
    return Patient.objects.filter(
        admission_date__lte=target_date
    ).exclude(
        assessment__timing='baseline'
    ).select_related('attending_physician')


def count_sessions_by_patient(patient_id: int, up_to_date: Optional[date] = None) -> int:
    """
    指定日までのセッション数をカウント
    """
    query = TreatmentSession.objects.filter(patient_id=patient_id)
    if up_to_date:
        query = query.filter(date__date__lte=up_to_date)
    return query.count()
