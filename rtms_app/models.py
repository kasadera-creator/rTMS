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
    notes = models.TextField("特記", blank=True)

class TreatmentSession(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    date = models.DateTimeField("日時", default=timezone.now)
    safety_sleep = models.BooleanField(default=True)
    safety_alcohol = models.BooleanField(default=True)
    safety_meds = models.BooleanField(default=True)
    motor_threshold = models.IntegerField("MT")
    intensity = models.IntegerField("強度", default=120)
    total_pulses = models.IntegerField("パルス", default=1980)
    side_effects = models.JSONField("副作用", default=dict, blank=True, null=True)
    performer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

class Assessment(models.Model):
    TIMING_CHOICES = [
        ('baseline', '治療前 (Base)'),
        ('week3', '3週目'),
        ('week6', '6週目'),
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