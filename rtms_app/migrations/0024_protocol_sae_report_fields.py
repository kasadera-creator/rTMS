from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('rtms_app', '0023_alter_assessment_timing_and_more'),
    ]

    operations = [
        # Patient.protocol_type
        migrations.AddField(
            model_name='patient',
            name='protocol_type',
            field=models.CharField(
                verbose_name='プロトコル',
                max_length=16,
                choices=[('INSURANCE', '保険診療プロトコル'), ('PMS', '市販後調査プロトコル')],
                default='INSURANCE',
            ),
        ),
        # AssessmentRecord improvements
        migrations.AddField(
            model_name='assessmentrecord',
            name='improvement_rate_17',
            field=models.FloatField(verbose_name='改善率17', null=True, blank=True),
        ),
        migrations.AddField(
            model_name='assessmentrecord',
            name='status_label',
            field=models.CharField(verbose_name='判定', max_length=16, blank=True, default=''),
        ),
        # SeriousAdverseEvent model
        migrations.CreateModel(
            name='SeriousAdverseEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('course_number', models.IntegerField(verbose_name='クール数', default=1, db_index=True)),
                ('event_types', models.JSONField(verbose_name='イベント種別', default=list, blank=True)),
                ('other_text', models.TextField(verbose_name='その他詳細', blank=True, default='')),
                ('auto_snapshot', models.JSONField(verbose_name='スナップショット', default=dict, blank=True, null=True)),
                ('created_at', models.DateTimeField(verbose_name='作成日時', auto_now_add=True)),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='rtms_app.patient')),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sae_records', to='rtms_app.treatmentsession')),
            ],
            options={
                'verbose_name': '重篤有害事象',
                'verbose_name_plural': '重篤有害事象',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='seriousadverseevent',
            constraint=models.UniqueConstraint(fields=['patient', 'course_number', 'session'], name='unique_sae_per_session'),
        ),
    ]
