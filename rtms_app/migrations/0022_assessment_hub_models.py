from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


TIMING_CHOICES = [
    ('baseline', '治療前評価'),
    ('week3', '3週目評価'),
    ('week4', '4週目評価'),
    ('week6', '6週目評価'),
    ('other', 'その他'),
]


def seed_scales_and_configs(apps, schema_editor):
    ScaleDefinition = apps.get_model('rtms_app', 'ScaleDefinition')
    TimingScaleConfig = apps.get_model('rtms_app', 'TimingScaleConfig')

    hamd, _created = ScaleDefinition.objects.get_or_create(
        code='hamd',
        defaults={'name': 'HAM-D', 'description': ''},
    )

    for timing, _label in TIMING_CHOICES:
        if timing == 'other':
            continue
        TimingScaleConfig.objects.get_or_create(
            timing=timing,
            scale=hamd,
            defaults={'is_enabled': True, 'display_order': 0},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('rtms_app', '0021_treatmentsession_meta'),
    ]

    operations = [
        migrations.CreateModel(
            name='ScaleDefinition',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.SlugField(max_length=32, unique=True, verbose_name='コード')),
                ('name', models.CharField(max_length=128, verbose_name='名称')),
                ('description', models.TextField(blank=True, verbose_name='説明')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='有効')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='作成日時')),
            ],
            options={
                'verbose_name': '尺度定義',
                'verbose_name_plural': '尺度定義',
                'ordering': ['code'],
            },
        ),
        migrations.CreateModel(
            name='TimingScaleConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timing', models.CharField(choices=TIMING_CHOICES, db_index=True, max_length=20, verbose_name='時期')),
                ('is_enabled', models.BooleanField(db_index=True, default=True, verbose_name='有効')),
                ('display_order', models.PositiveSmallIntegerField(default=0, verbose_name='表示順')),
                ('scale', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='timing_configs', to='rtms_app.scaledefinition')),
            ],
            options={
                'verbose_name': '尺度設定（時期）',
                'verbose_name_plural': '尺度設定（時期）',
                'ordering': ['timing', 'display_order', 'scale__code'],
            },
        ),
        migrations.CreateModel(
            name='AssessmentRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('course_number', models.IntegerField(db_index=True, default=1, verbose_name='クール数')),
                ('timing', models.CharField(choices=TIMING_CHOICES, db_index=True, max_length=20, verbose_name='時期')),
                ('date', models.DateField(default=django.utils.timezone.now, verbose_name='日')),
                ('scores', models.JSONField(default=dict, verbose_name='スコア')),
                ('total_score_21', models.IntegerField(default=0, verbose_name='合計21')),
                ('total_score_17', models.IntegerField(default=0, verbose_name='合計17')),
                ('note', models.TextField(blank=True, verbose_name='特記')),
                ('meta', models.JSONField(blank=True, default=dict, null=True, verbose_name='メタ')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='作成日時')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新日時')),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='rtms_app.patient')),
                ('scale', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='records', to='rtms_app.scaledefinition')),
            ],
            options={
                'verbose_name': '評価（新）',
                'verbose_name_plural': '評価（新）',
            },
        ),
        migrations.AddConstraint(
            model_name='timingscaleconfig',
            constraint=models.UniqueConstraint(fields=('timing', 'scale'), name='unique_timing_scale_config'),
        ),
        migrations.AddConstraint(
            model_name='assessmentrecord',
            constraint=models.UniqueConstraint(fields=('patient', 'course_number', 'timing', 'scale'), name='unique_assessment_record_per_patient_course_timing_scale'),
        ),
        migrations.AddIndex(
            model_name='assessmentrecord',
            index=models.Index(fields=['patient', 'timing'], name='rtms_app_as_patient_e156d2_idx'),
        ),
        migrations.AddIndex(
            model_name='assessmentrecord',
            index=models.Index(fields=['scale', 'timing'], name='rtms_app_as_scale_i_95a493_idx'),
        ),
        migrations.RunPython(seed_scales_and_configs, migrations.RunPython.noop),
    ]
