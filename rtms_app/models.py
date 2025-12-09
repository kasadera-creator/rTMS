from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User

class Patient(models.Model):
    """患者情報"""
    GENDER_CHOICES = [('M', '男性'), ('F', '女性'), ('O', 'その他')]
    
    # 基本情報
    card_id = models.CharField("カルテ番号", max_length=20, unique=True)
    name = models.CharField("氏名", max_length=100)
    birth_date = models.DateField("生年月日")
    gender = models.CharField("性別", max_length=1, choices=GENDER_CHOICES, default='M')
    
    # 診療情報
    attending_physician = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="担当医", related_name="patients")
    referral_source = models.CharField("紹介元", max_length=200, blank=True)
    diagnosis = models.CharField("診断名", max_length=200, default="うつ病")
    
    # 病歴・生活歴
    life_history = models.TextField("生活歴", blank=True)
    past_history = models.TextField("既往歴", blank=True)
    present_illness = models.TextField("現病歴", blank=True)
    medication_history = models.TextField("薬剤治療歴", blank=True)

    # ★復活・追加: エラー解消のため
    mapping_notes = models.TextField("位置決め記録メモ", blank=True)
    
    # スケジュール
    admission_date = models.DateField("入院予定日", null=True, blank=True)
    mapping_date = models.DateField("初回位置決め日", null=True, blank=True)
    first_treatment_date = models.DateField("初回治療日", null=True, blank=True)

    # 初診時適正質問票
    questionnaire_data = models.JSONField("適正質問票回答", default=dict, blank=True, null=True)
    
    created_at = models.DateTimeField("登録日", auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.card_id})"

    @property
    def age(self):
        """年齢計算"""
        today = timezone.now().date()
        return today.year - self.birth_date.year - ((today.month, today.day) < (self.birth_date.month, self.birth_date.day))

    class Meta:
        verbose_name = "患者情報"
        verbose_name_plural = "患者情報一覧"

# --- 以下、MappingSession, TreatmentSession, Assessment は変更なし（前回のままでOK） ---
class MappingSession(models.Model):
    WEEK_CHOICES = [(1, '第1週'), (2, '第2週'), (3, '第3週'), (4, 'その他')]
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, verbose_name="患者")
    date = models.DateField("実施日", default=timezone.now)
    week_number = models.IntegerField("時期", choices=WEEK_CHOICES, default=1)
    resting_mt = models.IntegerField("安静時MT(%)")
    stimulation_site = models.CharField("刺激部位", max_length=100, default="左DLPFC (Brainsway H1)")
    notes = models.TextField("特記事項", blank=True)

class TreatmentSession(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, verbose_name="患者")
    date = models.DateTimeField("実施日時", default=timezone.now)
    safety_sleep = models.BooleanField("睡眠不足なし", default=True)
    safety_alcohol = models.BooleanField("アルコール・カフェイン過剰なし", default=True)
    safety_meds = models.BooleanField("服薬変更なし", default=True)
    motor_threshold = models.IntegerField("MT(%)", help_text="当日の設定MT")
    intensity = models.IntegerField("刺激強度(%)", default=120)
    total_pulses = models.IntegerField("総パルス数", default=1980)
    side_effects = models.JSONField("副作用チェック詳細", default=dict, blank=True, null=True)
    performer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, verbose_name="実施者")

    def __str__(self):
        return f"{self.date.strftime('%Y-%m-%d')} - {self.patient.name}"

class Assessment(models.Model):
    ASSESSMENT_TYPES = [('HAM-D', 'HAM-D')]
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, verbose_name="患者")
    date = models.DateField("検査日", default=timezone.now)
    type = models.CharField("検査種別", max_length=20, choices=ASSESSMENT_TYPES, default='HAM-D')
    scores = models.JSONField("各項目スコア", default=dict)
    total_score_21 = models.IntegerField("合計点(21)", default=0)
    total_score_17 = models.IntegerField("合計点(17)", default=0)
    timing = models.CharField("評価時期", max_length=20, 
        choices=[('baseline', '治療前'), ('week3', '3週目'), ('week6', '6週目'), ('other', 'その他')],
        default='other')
    note = models.TextField("判定・コメント", blank=True)

    def calculate_scores(self):
        s = self.scores
        # HAM-D 17項目に含まれるKey (1-17)
        keys_17 = [str(i) for i in range(1, 18)]
        # 全21項目
        keys_21 = [str(i) for i in range(1, 22)]
        
        self.total_score_17 = sum(int(s.get(k, 0)) for k in keys_17)
        self.total_score_21 = sum(int(s.get(k, 0)) for k in keys_21)

    def save(self, *args, **kwargs):
        if self.type == 'HAM-D':
            self.calculate_scores()
        super().save(*args, **kwargs)