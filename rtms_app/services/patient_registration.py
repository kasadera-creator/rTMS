from copy import deepcopy

from django.db import transaction

from rtms_app.models import Patient, TreatmentCourse


STATUS_MAP = {
    "waiting": "waiting_admission",
    "inpatient": "inpatient_waiting_treatment",
    "discharged": "discharged",
}

COPY_FIELDS = (
    "diagnosis",
    "chief_complaint",
    "present_illness",
    "medication_history",
    "weight_kg",
    "is_weight_unknown",
    "attending_physician_id",
    "referral_source",
    "referral_doctor",
    "estimated_onset_year",
    "estimated_onset_month",
    "is_all_case_survey",
    "first_visit_date",
    "admission_date",
    "admission_type",
    "is_admission_procedure_done",
    "first_treatment_date",
    "mapping_date",
    "mapping_notes",
    "summary_text",
    "discharge_prescription",
    "discharge_date",
)

ADDITIONAL_COURSE_COPY_FIELDS = (
    "chief_complaint",
    "diagnosis",
    "life_history",
    "past_history",
    "present_illness",
    "medication_history",
    "has_other_psychiatric_history",
    "psychiatric_history",
    "psychiatric_history_other_text",
    "weight_kg",
    "is_weight_unknown",
    "attending_physician_id",
    "referral_source",
    "referral_doctor",
    "estimated_onset_year",
    "estimated_onset_month",
    "is_all_case_survey",
)


def ensure_initial_treatment_course(patient: Patient) -> TreatmentCourse:
    defaults = {field: getattr(patient, field) for field in COPY_FIELDS}
    defaults["course_status"] = STATUS_MAP[patient.status]
    defaults["course_end_reason"] = ""
    defaults["questionnaire_data"] = deepcopy(patient.questionnaire_data)
    course, _created = TreatmentCourse.objects.get_or_create(
        patient=patient,
        course_number=1,
        defaults=defaults,
    )
    return course


@transaction.atomic
def register_patient_with_initial_course(patient: Patient) -> tuple[Patient, TreatmentCourse]:
    patient.save()
    return patient, ensure_initial_treatment_course(patient)


@transaction.atomic
def register_additional_treatment_course(
    patient: Patient,
    *,
    course_number: int = None,
    overrides: dict = None,
) -> TreatmentCourse:
    """Create or retrieve an additional course without duplicating the patient."""
    locked_patient = Patient.objects.select_for_update().get(pk=patient.pk)
    latest_course_number = (
        TreatmentCourse.objects.filter(patient=locked_patient)
        .order_by("-course_number")
        .values_list("course_number", flat=True)
        .first()
    ) or 0
    requested_course_number = course_number or latest_course_number + 1
    if requested_course_number <= latest_course_number:
        existing = TreatmentCourse.objects.filter(
            patient=locked_patient,
            course_number=requested_course_number,
        ).first()
        if existing is not None:
            return existing
        raise ValueError("Requested course number is not the next course")
    if requested_course_number != latest_course_number + 1:
        raise ValueError("Course number must be the next available number")

    overrides = overrides or {}
    defaults = {
        field: deepcopy(getattr(locked_patient, field))
        for field in ADDITIONAL_COURSE_COPY_FIELDS
    }
    for field in ("referral_source", "referral_doctor", "first_visit_date"):
        if overrides.get(field) is not None:
            defaults[field] = overrides[field]
    defaults.update({
        "course_status": "waiting_admission",
        "course_end_reason": "",
        "admission_date": None,
        "first_treatment_date": None,
        "mapping_date": None,
        "discharge_date": None,
        "completed_at": None,
        "discharged_at": None,
        "summary_text": "",
        "discharge_prescription": "",
        "questionnaire_data": deepcopy(locked_patient.questionnaire_data),
    })
    course, _created = TreatmentCourse.objects.get_or_create(
        patient=locked_patient,
        course_number=requested_course_number,
        defaults=defaults,
    )
    locked_patient.course_number = requested_course_number
    locked_patient.save(update_fields=["course_number"])
    return course