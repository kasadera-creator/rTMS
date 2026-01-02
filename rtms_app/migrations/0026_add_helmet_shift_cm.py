# Generated manually: add helmet_shift_cm to TreatmentSession
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('rtms_app', '0025_add_adverse_event_report'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentsession',
            name='helmet_shift_cm',
            field=models.IntegerField(blank=True, default=6, help_text='MT測定位置から治療位置への移動量。通常は前方+6cm。', null=True, verbose_name='治療時ヘルメット移動（cm、前方+）'),
        ),
    ]
