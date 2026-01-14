from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rtms_app', '0030_assessment_performed_date_treatmentskip'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentskip',
            name='snapshot',
            field=models.JSONField(blank=True, null=True, default=dict, help_text='skip 実行前のセッション日・日時のスナップショット', verbose_name='影響スナップショット'),
        ),
    ]
