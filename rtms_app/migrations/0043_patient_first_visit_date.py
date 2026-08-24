# Generated manually for Stage 6. Existing rows remain NULL.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rtms_app', '0042_remove_patient_first_visit_date_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='patient',
            name='first_visit_date',
            field=models.DateField(blank=True, null=True, verbose_name='初診日'),
        ),
    ]
