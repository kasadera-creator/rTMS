from copy import deepcopy

from django.db import migrations, transaction


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


def populate_treatment_courses(apps, schema_editor):
    Patient = apps.get_model("rtms_app", "Patient")
    TreatmentCourse = apps.get_model("rtms_app", "TreatmentCourse")

    patients = list(Patient.objects.all().order_by("pk"))
    unknown_statuses = sorted({patient.status for patient in patients if patient.status not in STATUS_MAP})
    if unknown_statuses:
        raise RuntimeError(
            "Cannot populate TreatmentCourse: unsupported Patient.status values: "
            + ", ".join(unknown_statuses)
        )

    invalid_course_numbers = sorted({patient.course_number for patient in patients if patient.course_number != 1})
    if invalid_course_numbers:
        raise RuntimeError(
            "Cannot populate TreatmentCourse: Patient.course_number must be 1, found: "
            + ", ".join(str(value) for value in invalid_course_numbers)
        )

    with transaction.atomic():
        for patient in patients:
            defaults = {field: getattr(patient, field) for field in COPY_FIELDS}
            defaults["course_number"] = 1
            defaults["course_status"] = STATUS_MAP[patient.status]
            defaults["course_end_reason"] = ""
            defaults["questionnaire_data"] = deepcopy(patient.questionnaire_data)
            TreatmentCourse.objects.get_or_create(
                patient_id=patient.pk,
                course_number=1,
                defaults=defaults,
            )


def preserve_treatment_courses(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("rtms_app", "0044_treatmentcourse"),
    ]

    operations = [
        migrations.RunPython(populate_treatment_courses, preserve_treatment_courses),
    ]
