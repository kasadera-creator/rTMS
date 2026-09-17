from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rtms_app", "0053_rtmsadmissioncapacity_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="treatmentcourse",
            name="private_room_planned",
            field=models.BooleanField(default=False, verbose_name="個室利用予定"),
        ),
    ]