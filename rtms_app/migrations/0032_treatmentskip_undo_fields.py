from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rtms_app', '0031_treatmentskip_snapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentskip',
            name='undone_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='skips_undone', to='auth.user'),
        ),
        migrations.AddField(
            model_name='treatmentskip',
            name='undone_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='取り消し日時'),
        ),
    ]
