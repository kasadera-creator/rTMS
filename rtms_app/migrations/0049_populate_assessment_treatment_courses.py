from django.db import migrations, transaction


MODEL_NAMES = ("Assessment", "AssessmentRecord", "AssessmentSchedule")


def populate_assessment_treatment_courses(apps, schema_editor):
    TreatmentCourse = apps.get_model("rtms_app", "TreatmentCourse")

    with transaction.atomic():
        for model_name in MODEL_NAMES:
            model = apps.get_model("rtms_app", model_name)
            rows = list(model.objects.all().order_by("pk"))
            for row in rows:
                matches = list(
                    TreatmentCourse.objects.filter(
                        patient_id=row.patient_id,
                        course_number=row.course_number,
                    )
                )
                if len(matches) != 1:
                    raise RuntimeError(
                        "Cannot populate "
                        f"{model_name}.treatment_course: expected one matching "
                        f"TreatmentCourse for {model_name}={row.pk}, "
                        f"patient={row.patient_id}, course_number={row.course_number}; "
                        f"found {len(matches)}"
                    )

                course = matches[0]
                existing_course_id = getattr(row, "treatment_course_id", None)
                if existing_course_id is not None and existing_course_id != course.pk:
                    raise RuntimeError(
                        "Cannot populate "
                        f"{model_name}.treatment_course: existing FK for "
                        f"{model_name}={row.pk} points to TreatmentCourse="
                        f"{existing_course_id}, expected {course.pk}"
                    )
                if course.patient_id != row.patient_id:
                    raise RuntimeError(
                        "Cannot populate "
                        f"{model_name}.treatment_course: patient mismatch for "
                        f"{model_name}={row.pk}"
                    )
                if course.course_number != row.course_number:
                    raise RuntimeError(
                        "Cannot populate "
                        f"{model_name}.treatment_course: course_number mismatch for "
                        f"{model_name}={row.pk}"
                    )

                model.objects.filter(pk=row.pk).update(treatment_course_id=course.pk)


def preserve_assessment_treatment_courses(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("rtms_app", "0048_assessment_treatment_course_and_more"),
    ]

    operations = [
        migrations.RunPython(
            populate_assessment_treatment_courses,
            preserve_assessment_treatment_courses,
        ),
    ]
