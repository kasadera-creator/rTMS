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