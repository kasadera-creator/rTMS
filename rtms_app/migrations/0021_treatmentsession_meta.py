from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rtms_app', '0020_assessment_course_number_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentsession',
            name='meta',
            field=models.JSONField(blank=True, default=dict, null=True, verbose_name='メタ'),
        ),
    ]
