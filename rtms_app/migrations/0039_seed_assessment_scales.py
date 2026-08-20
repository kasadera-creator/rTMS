from django.db import migrations


SCALES = [
    ("hamd", "HAM-D", 0),
    ("bacs", "BACS", 10),
    ("phq9", "PHQ-9", 20),
    ("sass-j", "SASS-J", 30),
    ("bdi-ii", "BDI-II", 40),
    ("sds", "SDS", 50),
    ("stai-trait", "STAI特性", 60),
    ("stai-state", "STAI状態", 70),
    ("dai-10", "DAI-10", 80),
]


def seed_scales(apps, schema_editor):
    ScaleDefinition = apps.get_model("rtms_app", "ScaleDefinition")
    TimingScaleConfig = apps.get_model("rtms_app", "TimingScaleConfig")

    for code, name, order in SCALES:
        scale, _created = ScaleDefinition.objects.get_or_create(
            code=code,
            defaults={"name": name, "is_active": True},
        )
        if scale.name != name or not scale.is_active:
            scale.name = name
            scale.is_active = True
            scale.save(update_fields=["name", "is_active"])

        timings = ["baseline", "week3", "week4", "week6"] if code == "hamd" else ["baseline", "post"]
        for timing in timings:
            TimingScaleConfig.objects.update_or_create(
                timing=timing,
                scale=scale,
                defaults={"is_enabled": True, "display_order": order},
            )


def unseed_scales(apps, schema_editor):
    ScaleDefinition = apps.get_model("rtms_app", "ScaleDefinition")
    ScaleDefinition.objects.filter(code__in=[code for code, _name, _order in SCALES if code != "hamd"]).delete()


class Migration(migrations.Migration):
    dependencies = [("rtms_app", "0038_patient_first_visit_date")]

    operations = [migrations.RunPython(seed_scales, unseed_scales)]
