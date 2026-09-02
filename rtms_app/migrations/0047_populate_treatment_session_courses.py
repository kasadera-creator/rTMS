from django.db import migrations, transaction


def populate_treatment_session_courses(apps, schema_editor):
    TreatmentSession = apps.get_model("rtms_app", "TreatmentSession")
    TreatmentCourse = apps.get_model("rtms_app", "TreatmentCourse")

    sessions = list(TreatmentSession.objects.all().order_by("pk"))
    links = []
    for session in sessions:
        courses = TreatmentCourse.objects.filter(
            patient_id=session.patient_id,
            course_number=session.course_number,
        )
        if courses.count() != 1:
            raise RuntimeError(
                "Cannot populate TreatmentSession.treatment_course: "
                f"session {session.pk} has no unique matching TreatmentCourse "
                f"for patient={session.patient_id}, course_number={session.course_number}"
            )
        course = courses.get()
        if session.treatment_course_id is not None and session.treatment_course_id != course.pk:
            raise RuntimeError(
                "Cannot populate TreatmentSession.treatment_course: "
                f"session {session.pk} already points to course {session.treatment_course_id}, "
                f"expected {course.pk}"
            )
        links.append((session.pk, course.pk, course.patient_id, course.course_number))

    with transaction.atomic():
        for session_id, course_id, patient_id, course_number in links:
            TreatmentSession.objects.filter(pk=session_id).update(
                treatment_course_id=course_id,
            )


def preserve_treatment_session_courses(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("rtms_app", "0046_treatmentsession_treatment_course_and_more"),
    ]

    operations = [
        migrations.RunPython(
            populate_treatment_session_courses,
            preserve_treatment_session_courses,
        ),
    ]
