"""
印刷サービス層
ビジネスロジックとデータ取得を分離
"""
from django.shortcuts import get_object_or_404
from django.http import Http404, HttpResponseBadRequest
from rtms_app.models import Patient, Assessment, TreatmentSession
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class PrintValidationError(Exception):
    """印刷時のバリデーションエラー（400用）"""
    pass


def validate_print_docs(docs: List[str]) -> None:
    """
    印刷ドキュメント指定のバリデーション
    不正な場合は PrintValidationError を raise
    """
    VALID_DOCS = {
        'admission', 'questionnaire', 'discharge', 
        'referral', 'path'
    }
    
    if not docs:
        raise PrintValidationError("印刷するドキュメントが指定されていません")
    
    invalid_docs = [doc for doc in docs if doc not in VALID_DOCS]
    if invalid_docs:
        raise PrintValidationError(
            f"不正なドキュメント指定: {', '.join(invalid_docs)}"
        )


def get_patient_for_print(patient_id: int) -> Patient:
    """
    印刷用の患者データ取得（404処理込み）
    """
    return get_object_or_404(
        Patient.objects.select_related('attending_physician'),
        pk=patient_id
    )


def build_print_context(patient: Patient, docs: List[str]) -> Dict:
    """
    印刷用のコンテキスト構築
    読み取り専用・副作用なし
    """
    context = {
        'patient': patient,
        'docs': docs,
        'today': None,  # Template側で設定
    }
    
    # 各ドキュメントに必要なデータを取得
    if 'path' in docs:
        from rtms_app.services.calender import generate_calendar_weeks
        context['calendar_data'] = generate_calendar_weeks(patient)
    
    if 'discharge' in docs or 'referral' in docs:
        # 尺度データ取得
        assessments = Assessment.objects.filter(
            patient=patient
        ).order_by('date', 'timing')
        context['assessments'] = assessments
    
    return context


def get_clinical_path_context(patient: Patient) -> Dict:
    """
    クリニカルパス印刷用のコンテキスト
    """
    from rtms_app.services.calender import generate_calendar_weeks
    
    return {
        'patient': patient,
        'calendar_data': generate_calendar_weeks(patient),
        'today': None,  # Template側で設定
    }
