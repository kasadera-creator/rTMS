from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rtms_app", "0055_rtmswaitlistentry_unique_active_waitlist_per_treatment_course"),
    ]

    operations = [
        migrations.AddField(
            model_name="treatmentcourse",
            name="planned_treatment_sessions",
            field=models.PositiveIntegerField(default=30, verbose_name="予定治療回数（回）"),
        ),
    ]