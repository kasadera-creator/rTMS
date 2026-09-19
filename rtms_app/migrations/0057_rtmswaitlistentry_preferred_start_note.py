from django.db import migrations, models


def forwards_migrate_waitlist_preferences(apps, schema_editor):
    waitlist_entry_model = apps.get_model("rtms_app", "RtmSWaitlistEntry")
    for entry in waitlist_entry_model.objects.all().iterator():
        parts = []
        if entry.preferred_start_from and entry.preferred_start_to:
            start = entry.preferred_start_from
            end = entry.preferred_start_to
            parts.append(
                f"{start.year}/{start.month}/{start.day}〜"
                f"{end.month}/{end.day}希望"
            )
        elif entry.preferred_start_from:
            start = entry.preferred_start_from
            parts.append(f"{start.year}/{start.month}/{start.day}以降希望")
        elif entry.preferred_start_to:
            end = entry.preferred_start_to
            parts.append(f"{end.year}/{end.month}/{end.day}まで希望")
        if entry.estimated_inpatient_days:
            parts.append(f"入院{entry.estimated_inpatient_days}日程度")
        if parts:
            entry.preferred_start_note = "、".join(parts)
            entry.save(update_fields=["preferred_start_note"])


def backwards_migrate_waitlist_preferences(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("rtms_app", "0056_treatmentcourse_planned_treatment_sessions"),
    ]

    operations = [
        migrations.AddField(
            model_name="rtmswaitlistentry",
            name="preferred_start_note",
            field=models.TextField(
                blank=True,
                default="",
                verbose_name="治療開始希望時期・備考",
            ),
        ),
        migrations.RunPython(
            forwards_migrate_waitlist_preferences,
            backwards_migrate_waitlist_preferences,
        ),
        migrations.RemoveConstraint(
            model_name="rtmswaitlistentry",
            name="waitlist_preferred_start_to_gte_from",
        ),
        migrations.RemoveField(
            model_name="rtmswaitlistentry",
            name="preferred_start_from",
        ),
        migrations.RemoveField(
            model_name="rtmswaitlistentry",
            name="preferred_start_to",
        ),
        migrations.RemoveField(
            model_name="rtmswaitlistentry",
            name="estimated_inpatient_days",
        ),
        migrations.RemoveField(
            model_name="rtmswaitlistentry",
            name="preferred_treatment_start",
        ),
    ]
