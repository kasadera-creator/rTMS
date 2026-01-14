from datetime import date

CONTENT_LABELS = {
    'admission': '入院時サマリー',
    'discharge': '退院時サマリー',
    'referral': '紹介状',
    'suitability': 'rTMS問診票',
    'side_effect': '治療実施記録票',
    'adverse_event': '有害事象報告書',
    'path': '臨床経過表',
    'bundle': 'ドキュメントバンドル',
}


def build_pdf_filename(patient, course_no, content_label, target_date: date):
    """Return filename like: {card_id}_{course}_{label}_{YYYY-MM-DD}.pdf

    `patient` may be a Patient instance; `course_no` fallback to 1 if None.
    `content_label` should be a fixed string (Japanese) as defined above or passed explicitly.
    """
    cid = getattr(patient, 'card_id', None) or getattr(patient, 'id', '')
    course_no = course_no or getattr(patient, 'course_number', 1) or 1
    dstr = target_date.isoformat() if target_date else date.today().isoformat()
    # sanitize label (replace spaces with underscore)
    label_safe = str(content_label).replace(' ', '_')
    return f"{cid}_{course_no}_{label_safe}_{dstr}.pdf"
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
