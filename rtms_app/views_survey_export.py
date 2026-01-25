"""
Staff-only CSV export views for patient surveys.
Separated to avoid circular import issues.
"""
import csv
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from .models import Patient, PatientSurveySession


@login_required
def export_patient_surveys_csv(request, patient_id):
    """Export all survey sessions for a patient to CSV."""
    if not request.user.is_staff:
        return HttpResponse("Forbidden", status=403)

    patient = get_object_or_404(Patient, pk=patient_id)
    sessions = PatientSurveySession.objects.filter(patient=patient).prefetch_related("responses").order_by("started_at")

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = f'attachment; filename="patient_{patient_id:05d}_surveys.csv"'
    writer = csv.writer(response)
    header = [
        "patient_id",
        "phase",
        "started_at",
        "submitted_at",
        "status",
        "bdi2_total",
        "sds_total",
        "sassj_total",
        "phq9_total",
        "phq9_q10",
        "stai_x1_total",
        "stai_x2_total",
        "dai10_total",
    ]
    writer.writerow(header)

    for session in sessions:
        resp_map = {r.instrument: r for r in session.responses.all()}

        def total_for(code: str):
            r = resp_map.get(code)
            return r.total_score if r else ''

        phq9_q10 = resp_map.get("phq9").phq9_difficulty if resp_map.get("phq9") else ''

        writer.writerow([
            f"{patient.id:05d}",
            session.phase,
            session.started_at,
            session.submitted_at,
            session.status,
            total_for("bdi2"),
            total_for("sds"),
            total_for("sassj"),
            total_for("phq9"),
            phq9_q10,
            total_for("stai_x1"),
            total_for("stai_x2"),
            total_for("dai10"),
        ])

    return response
