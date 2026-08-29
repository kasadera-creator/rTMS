"""
Superuser-only CSV export views for research data.
Separated to avoid circular import issues (mirrors views_survey_export.py).
"""
import io
import zipfile

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.utils import timezone

from .models import AuditLog
from .services.export_research import (
    generate_research_adverse_events_csv,
    generate_research_summary_csv,
    generate_research_treatment_detail_csv,
)
from .utils.request_context import get_client_ip, get_user_agent


def _require_superuser(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Forbidden: superuser only")
    return None


def _log_export(request, target_model, summary):
    AuditLog.objects.create(
        user=request.user,
        patient=None,
        target_model=target_model,
        target_pk='',
        action='EXPORT',
        summary=summary,
        meta={},
        ip=get_client_ip(request),
        user_agent=get_user_agent(request),
    )


def _csv_response(content, filename):
    response = HttpResponse(content.encode('utf-8-sig'), content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
def export_research_summary_csv(request):
    """research_summary.csv: 1 row = 1 (card_id, course_number)."""
    forbidden = _require_superuser(request)
    if forbidden:
        return forbidden
    content = generate_research_summary_csv()
    _log_export(request, 'ResearchSummaryCSV', '研究用サマリーCSVエクスポート')
    return _csv_response(content, 'research_summary.csv')


@login_required
def export_research_treatment_detail_csv(request):
    """research_treatment_detail.csv: 1 row = 1 TreatmentSession."""
    forbidden = _require_superuser(request)
    if forbidden:
        return forbidden
    content = generate_research_treatment_detail_csv()
    _log_export(request, 'ResearchTreatmentDetailCSV', '研究用治療詳細CSVエクスポート')
    return _csv_response(content, 'research_treatment_detail.csv')


@login_required
def export_research_adverse_events_csv(request):
    """research_adverse_events.csv: 1 row = 1 SeriousAdverseEvent."""
    forbidden = _require_superuser(request)
    if forbidden:
        return forbidden
    content = generate_research_adverse_events_csv()
    _log_export(request, 'ResearchAdverseEventsCSV', '研究用有害事象CSVエクスポート')
    return _csv_response(content, 'research_adverse_events.csv')


@login_required
def export_research_zip(request):
    """All three research CSVs bundled into one ZIP download."""
    forbidden = _require_superuser(request)
    if forbidden:
        return forbidden

    files = {
        'research_summary.csv': generate_research_summary_csv(),
        'research_treatment_detail.csv': generate_research_treatment_detail_csv(),
        'research_adverse_events.csv': generate_research_adverse_events_csv(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content.encode('utf-8-sig'))

    _log_export(request, 'ResearchExportZip', '研究用データCSV一括エクスポート（ZIP）')

    timestamp = timezone.localtime().strftime('%Y%m%d_%H%M%S')
    response = HttpResponse(buffer.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="research_export_{timestamp}.zip"'
    return response
