from django.db import models
from django.utils import timezone

class Patient(models.Model):
    """患者情報"""
    card_id = models.CharField("カルテ番号", max_length=20, unique=True)
    name = models.CharField("氏名", max_length=100)
    birth_date = models.DateField("生年月日")
    diagnosis = models.CharField("診断名", max_length=200, default="うつ病")
    
    # 既往歴やチェックリスト（柔軟に追加可能にするためJSON推奨）
    medical_history = models.JSONField("既往歴・禁忌チェック", default=dict, blank=True)
    created_at = models.DateTimeField("登録日", auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.card_id})"

class TreatmentSession(models.Model):
    """日々の治療実施記録 (Brainsway H1コイル)"""
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, verbose_name="患者")
    date = models.DateTimeField("実施日時", default=timezone.now)
    
    # [cite_start]治療前安全確認 [cite: 85]
    safety_sleep = models.BooleanField("睡眠不足なし", default=True)
    safety_alcohol = models.BooleanField("アルコール・カフェイン過剰なし", default=True)
    safety_meds = models.BooleanField("服薬変更なし", default=True)
    
    # [cite_start]治療パラメータ（初期値はBrainsway標準設定 [cite: 75]）
    motor_threshold = models.IntegerField("MT(%)", help_text="安静時運動閾値")
    intensity = models.IntegerField("刺激強度(%)", default=120, help_text="通常120%")
    frequency = models.IntegerField("周波数(Hz)", default=18)
    duration_sec = models.IntegerField("刺激時間(秒)", default=2)
    interval_sec = models.IntegerField("刺激間隔(秒)", default=20)
    total_pulses = models.IntegerField("総パルス数", default=1980)
    
    # 実施後観察
    adverse_events = models.JSONField("有害事象・観察", default=dict, blank=True)
    # 例: {"headache": "なし", "seizure_signs": "なし"}

    def __str__(self):
        return f"{self.date.strftime('%Y-%m-%d')} - {self.patient.name}"