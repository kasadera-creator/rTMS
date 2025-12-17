from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
import os

class Patient(models.Model):
    GENDER_CHOICES = [('M', '男性'), ('F', '女性'), ('O', 'その他')]
    ADMISSION_TYPES = [('voluntary', '任意入院'), ('medical_protection', '医療保護入院'), ('emergency', '緊急措置入院'), ('measure', '措置入院')]
    
    # ★変更: unique=True を削除（複数クール対応のため）
    card_id = models.CharField("カルテ番号", max_length=20) 
    # ★追加: 何クール目か
    course_number = models.IntegerField("クール数", default=1)
    
    name = models.CharField("氏名", max_length=100)
    birth_date = models.DateField("生年月日")
    gender = models.CharField("性別", max_length=1, choices=GENDER_CHOICES, default='M')
    attending_physician = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="担当医")
    
    referral_source = models.CharField("紹介元医療機関", max_length=200, blank=True)
    referral_doctor = models.CharField("紹介医", max_length=100, blank=True)
    
    chief_complaint = models.CharField("主訴", max_length=200, blank=True)
    diagnosis = models.CharField("診断名", max_length=200, default="うつ病")
    
    life_history = models.TextField("生活歴", blank=True)
    past_history = models.TextField("既往歴", blank=True)
    present_illness = models.TextField("現病歴", blank=True)
    medication_history = models.TextField("薬剤治療歴", blank=True)
    
    summary_text = models.TextField("サマリー本文", blank=True)
    discharge_prescription = models.TextField("退院時処方", blank=True)
    discharge_date = models.DateField("退院日", null=True, blank=True)

    mapping_notes = models.TextField("位置決め記録メモ", blank=True)
    
    admission_date = models.DateField("入院予定日", null=True, blank=True)
    mapping_date = models.DateField("初回位置決め日", null=True, blank=True)
    first_treatment_date = models.DateField("初回治療日", null=True, blank=True)
    
    admission_type = models.CharField("入院形態", max_length=20, choices=ADMISSION_TYPES, default='voluntary')
    is_admission_procedure_done = models.BooleanField("入院手続き完了", default=False)
    questionnaire_data = models.JSONField("適正質問票", default=dict, blank=True, null=True)
    created_at = models.DateTimeField("登録日", auto_now_add=True)
    
    STATUS_CHOICES = [
        ("waiting", "入院待ち"),
        ("inpatient", "入院中"),
        ("discharged", "退院済"),
    ]
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="waiting", db_index=True)

    def __str__(self): return f"{self.name} ({self.card_id} - {self.course_number}クール)"
    @property
    def age(self):
        today = timezone.now().date()
        return today.year - self.birth_date.year - ((today.month, today.day) < (self.birth_date.month, self.birth_date.day))
        
    class Meta:
        verbose_name = "患者"
        verbose_name_plural = "患者"


def consent_upload_to(instance, filename):
    # 拡張子を維持（.pdf想定）
    ext = os.path.splitext(filename)[1].lower() or ".pdf"
    date_str = timezone.localdate().strftime("%Y%m%d")
    # ファイル名固定ルール
    return f"consent/rTMS-ICF_{date_str}{ext}"


class ConsentDocument(models.Model):
    file = models.FileField(upload_to=consent_upload_to)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
        verbose_name = "説明同意書"
        verbose_name_plural = "説明同意書"

    def __str__(self):
        return f"ConsentDocument {self.uploaded_at:%Y-%m-%d}"


class MappingSession(models.Model):
    WEEK_CHOICES = [
        (1, '第1週'), (2, '第2週'), (3, '第3週'), 
        (4, '第4週'), (5, '第5週'), (6, '第6週'), 
        (99, 'その他')
    ]
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    date = models.DateField("実施日", default=timezone.now)
    week_number = models.IntegerField("時期", choices=WEEK_CHOICES, default=1)
    resting_mt = models.IntegerField("MT")
    stimulation_site = models.CharField("部位", max_length=100, default="左DLPFC")
    
    # 刺激強度パラメータ
    stimulus_intensity_mt_percent = models.IntegerField("刺激強度MT%", null=True, blank=True, help_text="例: 120")
    intensity_percent = models.IntegerField("強度%", null=True, blank=True, help_text="例: 60")
    
    # ヘルメット位置 a
    helmet_position_a_x = models.DecimalField("ヘルメット位置a X", max_digits=5, decimal_places=2, null=True, blank=True, help_text="例: 3.00")
    helmet_position_a_y = models.DecimalField("ヘルメット位置a Y", max_digits=5, decimal_places=2, null=True, blank=True, help_text="例: 1.00")
    
    # ヘルメット位置 b
    helmet_position_b_x = models.DecimalField("ヘルメット位置b X", max_digits=5, decimal_places=2, null=True, blank=True, help_text="例: 9.00")
    helmet_position_b_y = models.DecimalField("ヘルメット位置b Y", max_digits=5, decimal_places=2, null=True, blank=True, help_text="例: 1.00")
    
    notes = models.TextField("特記", blank=True)
    
    class Meta:
        verbose_name = "位置決めセッション"
        verbose_name_plural = "位置決めセッション"

class TreatmentSession(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    date = models.DateTimeField("日時", default=timezone.now)
    safety_sleep = models.BooleanField(default=True)
    safety_alcohol = models.BooleanField(default=True)
    safety_meds = models.BooleanField(default=True)

    # 当日治療パラメータ（印刷用にも利用）
    coil_type = models.CharField(max_length=32, blank=True, default="H1")
    target_site = models.CharField(max_length=64, blank=True, default="左背外側前頭前野")

    mt_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    intensity_percent = models.PositiveSmallIntegerField(null=True, blank=True)

    frequency_hz = models.DecimalField(max_digits=5, decimal_places=1, default=18.0)
    train_seconds = models.DecimalField(max_digits=5, decimal_places=1, default=2.0)
    intertrain_seconds = models.DecimalField(max_digits=5, decimal_places=1, default=20.0)
    train_count = models.PositiveSmallIntegerField("トレイン数", default=55)
    total_pulses = models.PositiveIntegerField(default=1980)
    sessions_per_day = models.PositiveSmallIntegerField(default=1)

    treatment_notes = models.TextField(blank=True, default="")

    # 互換性確保（旧UIの値を上書き）
    motor_threshold = models.IntegerField("MT", null=True, blank=True)
    intensity = models.IntegerField("強度", null=True, blank=True)

    side_effects = models.JSONField("副作用", default=dict, blank=True, null=True)
    performer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    class Meta:
        verbose_name = "治療セッション"
        verbose_name_plural = "治療セッション"


class SideEffectCheck(models.Model):
    session = models.OneToOneField("TreatmentSession", on_delete=models.CASCADE, related_name="side_effect_check")
    rows = models.JSONField(default=list, blank=True)
    memo = models.TextField(blank=True, default="")
    physician_signature = models.CharField(max_length=128, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"SideEffectCheck(session={self.session_id})"

class Assessment(models.Model):
    TIMING_CHOICES = [
        ('baseline', '治療前評価'),
        ('week3', '3週目評価'),
        ('week6', '6週目評価'),
        ('other', 'その他'),
    ]
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    date = models.DateField("日", default=timezone.now)
    type = models.CharField("種別", max_length=20, default='HAM-D')
    scores = models.JSONField("スコア", default=dict)
    total_score_21 = models.IntegerField("合計21", default=0)
    total_score_17 = models.IntegerField("合計17", default=0)
    timing = models.CharField("時期", max_length=20, choices=TIMING_CHOICES, default='other')
    note = models.TextField("特記", blank=True)
    
    def calculate_scores(self):
        keys17 = [f"q{i}" for i in range(1, 18)]
        keys21 = [f"q{i}" for i in range(1, 22)]
        self.total_score_17 = sum(int(self.scores.get(k, 0)) for k in keys17)
        self.total_score_21 = sum(int(self.scores.get(k, 0)) for k in keys21)
        
    def save(self, *args, **kwargs):
        if self.type == 'HAM-D': self.calculate_scores()
        super().save(*args, **kwargs)
        
    class Meta:
        verbose_name = "評価"
        verbose_name_plural = "評価"

class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('CREATE', '作成'),
        ('UPDATE', '更新'),
        ('DELETE', '削除'),
        ('PRINT', '印刷'),
        ('EXPORT', 'エクスポート'),
    ]
    
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="ユーザー")
    patient = models.ForeignKey(Patient, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="患者")
    target_model = models.CharField("対象モデル", max_length=100)
    target_pk = models.CharField("対象PK", max_length=50)
    action = models.CharField("アクション", max_length=10, choices=ACTION_CHOICES)
    summary = models.TextField("概要")
    meta = models.JSONField("メタデータ", default=dict)
    ip = models.GenericIPAddressField("IPアドレス", null=True, blank=True)
    user_agent = models.TextField("ユーザーエージェント", blank=True)
    
    def __str__(self):
        return f"{self.created_at} - {self.user} - {self.action} on {self.target_model}:{self.target_pk}"
    
    class Meta:
        verbose_name = "監査ログ"
        verbose_name_plural = "監査ログ"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_at']),
            models.Index(fields=['patient']),
            models.Index(fields=['action']),
        ]