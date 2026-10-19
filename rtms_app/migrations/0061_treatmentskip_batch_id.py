from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rtms_app', '0060_rtms_treatment_reservation'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentskip',
            name='batch_id',
            field=models.UUIDField(blank=True, db_index=True, null=True, verbose_name='中止バッチID'),
        ),
    ]