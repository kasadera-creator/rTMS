# Generated minimal migration to add status to TreatmentSession
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('rtms_app', '0026_add_helmet_shift_cm'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentsession',
            name='status',
            field=models.CharField(choices=[('planned', '予定'), ('done', '実施'), ('skipped', 'スキップ')], default='planned', max_length=16, db_index=True),
        ),
    ]
