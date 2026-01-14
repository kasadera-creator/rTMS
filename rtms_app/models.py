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
    # protocol_type was removed (no longer used). See migrations for removal.

    # --- 新規フィールド: 全例調査対象フラグ ---
    is_all_case_survey = models.BooleanField("全例調査対象", default=False)

    # --- 原疾患（うつ病）の推定発症年月 ---
    estimated_onset_year = models.IntegerField("原疾患（うつ病）推定発症年", null=True, blank=True)
    estimated_onset_month = models.IntegerField("原疾患（うつ病）推定発症月", null=True, blank=True)

    # --- 体重（研究項目） ---
    weight_kg = models.DecimalField("体重 (kg)", max_digits=4, decimal_places=1, null=True, blank=True)
    is_weight_unknown = models.BooleanField("体重不明", default=False)

    # --- 診断名（既往精神疾患）関連 ---
    HAS_PSY_CHOICES = [
        ("yes", "有"),
        ("no", "無"),
        ("unknown", "不明"),
    ]
    has_other_psychiatric_history = models.CharField(
        "診断名（今回 rTMS の適応以外の精神疾患の既往）",
        max_length=8,
        choices=HAS_PSY_CHOICES,
        default="no",
    )

    # List of selected psychiatric history codes/names; JSON list for flexibility and CSV export
    psychiatric_history = models.JSONField("既往精神疾患", default=list, blank=True, null=True)
    psychiatric_history_other_text = models.TextField("既往精神疾患（その他）", blank=True, default="")
    
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
        (7, '第7週'), (8, '第8週')
    ]
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    # クール数を記録（自然キーの一部）
    course_number = models.IntegerField("クール数", default=1, db_index=True)
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
        constraints = [
            models.UniqueConstraint(fields=['patient', 'course_number', 'date', 'stimulation_site'], name='unique_mapping_per_patient_course_date_site')
        ]

class TreatmentSession(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    # 日時は詳細情報として保持しつつ、レコードの自然キーとして日付部分を別管理
    date = models.DateTimeField("日時", default=timezone.now)
    # クール数を記録（自然キーの一部）
    course_number = models.IntegerField("クール数", default=1, db_index=True)
    # 実施日は日付単位で一意判定するために保持
    session_date = models.DateField("実施日", default=timezone.now, db_index=True)
    # 将来的な1日複数回対応用スロット
    slot = models.CharField("スロット", max_length=8, blank=True, default="")
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
    # 治療時ヘルメット移動量（cm、前方+）。通常は +6cm。
    helmet_shift_cm = models.IntegerField(
        "治療時ヘルメット移動（cm、前方+）",
        default=6,
        null=True,
        blank=True,
        help_text="MT測定位置から治療位置への移動量。通常は前方+6cm。",
    )

    treatment_notes = models.TextField(blank=True, default="")

    STATUS_CHOICES = [
        ("planned", "予定"),
        ("done", "実施"),
        ("skipped", "スキップ"),
    ]
    status = models.CharField("ステータス", max_length=16, choices=STATUS_CHOICES, default="planned", db_index=True)

    # 互換性確保（旧UIの値を上書き）
    motor_threshold = models.IntegerField("MT", null=True, blank=True)
    intensity = models.IntegerField("強度", null=True, blank=True)

    side_effects = models.JSONField("副作用", default=dict, blank=True, null=True)
    meta = models.JSONField("メタ", default=dict, blank=True, null=True)
    performer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    @property
    def stimulation_minutes(self):
        """総刺激時間（分）を算出: ((train_seconds + intertrain_seconds) * train_count - intertrain_seconds) / 60

        例: ((2 + 20) * 55 - 20) / 60 -> 19.833...
        """
        try:
            ts = float(self.train_seconds or 0)
            iti = float(self.intertrain_seconds or 0)
            n = int(self.train_count or 0)
            if n <= 0:
                return 0.0
            total_seconds = (ts + iti) * n - iti
            return total_seconds / 60.0
        except Exception:
            return None

    @property
    def stimulation_minutes_display(self):
        v = self.stimulation_minutes
        if v is None:
            return ''
        return f"{v:.2f}"

    def has_sae(self):
        """重篤有害事象レコードが存在するかを判定"""
        return hasattr(self, 'sae_records') and self.sae_records.exists()

    class Meta:
        verbose_name = "治療セッション"
        verbose_name_plural = "治療セッション"
        constraints = [
            models.UniqueConstraint(fields=['patient', 'course_number', 'session_date', 'slot'], name='unique_treatment_per_patient_course_date_slot')
        ]


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
        ('week4', '4週目評価'),
        ('week6', '6週目評価'),
        ('other', 'その他'),
    ]
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    # クール数を記録（自然キーの一部）
    course_number = models.IntegerField("クール数", default=1, db_index=True)
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
        constraints = [
            models.UniqueConstraint(fields=['patient', 'course_number', 'timing', 'type'], name='unique_assessment_per_patient_course_timing_type')
        ]


class ScaleDefinition(models.Model):
    """Master data for assessment scales (e.g., HAM-D).

    This exists to support multiple scales without changing core workflows.
    """

    code = models.SlugField("コード", max_length=32, unique=True)
    name = models.CharField("名称", max_length=128)
    description = models.TextField("説明", blank=True)
    is_active = models.BooleanField("有効", default=True, db_index=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)

    class Meta:
        verbose_name = "尺度定義"
        verbose_name_plural = "尺度定義"
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code}: {self.name}"


class TimingScaleConfig(models.Model):
    """Global configuration: which scales are enabled for each timing."""

    timing = models.CharField("時期", max_length=20, choices=Assessment.TIMING_CHOICES, db_index=True)
    scale = models.ForeignKey(ScaleDefinition, on_delete=models.CASCADE, related_name="timing_configs")
    is_enabled = models.BooleanField("有効", default=True, db_index=True)
    display_order = models.PositiveSmallIntegerField("表示順", default=0)

    class Meta:
        verbose_name = "尺度設定（時期）"
        verbose_name_plural = "尺度設定（時期）"
        ordering = ["timing", "display_order", "scale__code"]
        constraints = [
            models.UniqueConstraint(fields=["timing", "scale"], name="unique_timing_scale_config"),
        ]

    def __str__(self) -> str:
        return f"{self.get_timing_display()} - {self.scale.code} ({'ON' if self.is_enabled else 'OFF'})"


class AssessmentRecord(models.Model):
    """New extensible assessment record.

    For compatibility, the legacy `Assessment` model remains in use elsewhere.
    """

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    course_number = models.IntegerField("クール数", default=1, db_index=True)
    timing = models.CharField("時期", max_length=20, choices=Assessment.TIMING_CHOICES, db_index=True)
    scale = models.ForeignKey(ScaleDefinition, on_delete=models.PROTECT, related_name="records")
    date = models.DateField("日", default=timezone.now)
    scores = models.JSONField("スコア", default=dict)
    total_score_21 = models.IntegerField("合計21", default=0)
    total_score_17 = models.IntegerField("合計17", default=0)
    improvement_rate_17 = models.FloatField("改善率17", null=True, blank=True)
    status_label = models.CharField("判定", max_length=16, blank=True, default="")
    note = models.TextField("特記", blank=True)
    meta = models.JSONField("メタ", default=dict, blank=True, null=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    def calculate_scores(self):
        # Only HAM-D is supported initially
        if not self.scale_id:
            return
        if self.scale.code != 'hamd':
            return
        keys17 = [f"q{i}" for i in range(1, 18)]
        keys21 = [f"q{i}" for i in range(1, 22)]
        self.total_score_17 = sum(int(self.scores.get(k, 0)) for k in keys17)
        self.total_score_21 = sum(int(self.scores.get(k, 0)) for k in keys21)

    def save(self, *args, **kwargs):
        self.calculate_scores()
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "評価（新）"
        verbose_name_plural = "評価（新）"
        constraints = [
            models.UniqueConstraint(
                fields=['patient', 'course_number', 'timing', 'scale'],
                name='unique_assessment_record_per_patient_course_timing_scale',
            )
        ]
        indexes = [
            models.Index(fields=['patient', 'timing']),
            models.Index(fields=['scale', 'timing']),
        ]

class SeriousAdverseEvent(models.Model):
    """Serious adverse event tied to a treatment session.

    Captures the event types and a snapshot of conditions at the time.
    """
    EVENT_CHOICES = [
        ("seizure", "けいれん発作"),
        ("finger_muscle", "手指の筋収縮"),
        ("syncope", "失神"),
        ("mania", "躁病・軽躁病の出現"),
        ("suicide_attempt", "自殺企図"),
        ("other", "その他"),
    ]

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    course_number = models.IntegerField("クール数", default=1, db_index=True)
    session = models.ForeignKey(TreatmentSession, on_delete=models.CASCADE, related_name="sae_records")
    event_types = models.JSONField("イベント種別", default=list, blank=True)
    other_text = models.TextField("その他詳細", blank=True, default="")
    auto_snapshot = models.JSONField("スナップショット", default=dict, blank=True, null=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)

    class Meta:
        verbose_name = "重篤有害事象"
        verbose_name_plural = "重篤有害事象"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["patient", "course_number", "session"], name="unique_sae_per_session")
        ]

    def __str__(self) -> str:
        return f"SAE patient={self.patient_id} session={self.session_id} {self.event_types}"

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


class AdverseEventReport(models.Model):
    """有害事象報告書（1セッション=1報告書想定）"""
    
    DIAGNOSIS_CHOICES = [
        ("depressive_episode", "うつ病エピソード"),
        ("recurrent_depressive_disorder", "反復性うつ病性障害"),
        ("other", "その他"),
    ]
    
    OUTCOME_CHOICES = [
        ("recovery", "回復"),
        ("improvement", "軽快"),
        ("not_recovered", "未回復"),
        ("sequelae", "後遺症"),
        ("death", "死亡"),
        ("unknown", "不明"),
    ]
    
    SITE_CHOICES = [
        ("left_dlpfc", "左DLPFC"),
        ("other", "その他"),
    ]
    
    # ForeignKey
    session = models.OneToOneField(TreatmentSession, on_delete=models.CASCADE, related_name="adverse_event_report", verbose_name="治療セッション")
    
    # 事象タイプ（複数選択を保持）
    event_types = models.JSONField("有害事象種別", default=list, blank=True, help_text="['seizure', 'finger_muscle', 'syncope', 'mania', 'suicide_attempt', 'other'] など")
    
    # 基本情報
    adverse_event_name = models.CharField("有害事象名", max_length=200, blank=True)
    onset_date = models.DateField("発現日", null=True, blank=True)
    
    # 患者情報
    age = models.IntegerField("年齢", null=True, blank=True)
    sex = models.CharField("性別", max_length=10, blank=True)
    initials = models.CharField("イニシャル", max_length=20, blank=True)
    
    # 診断
    diagnosis_category = models.CharField("診断分類", max_length=40, choices=DIAGNOSIS_CHOICES, default="depressive_episode")
    diagnosis_other_text = models.CharField("診断その他", max_length=200, blank=True)
    
    # 併用情報
    concomitant_meds_text = models.TextField("併用薬", blank=True)
    substance_intake_text = models.TextField("薬物・アルコール・カフェイン摂取", blank=True)
    
    # 既往情報
    seizure_history_flag = models.BooleanField("てんかん・けいれん発作既往", default=False)
    seizure_history_date_text = models.CharField("けいれん発作既往時期", max_length=200, blank=True)
    
    # 治療パラメータ
    rmt_value = models.IntegerField("安静運動閾値", null=True, blank=True, help_text="% 単位は不要")
    intensity_value = models.IntegerField("刺激強度", null=True, blank=True, help_text="% 単位は不要")
    stimulation_site_category = models.CharField("刺激部位分類", max_length=20, choices=SITE_CHOICES, default="left_dlpfc")
    stimulation_site_other_text = models.CharField("刺激部位その他", max_length=100, blank=True)
    
    # 治療進捗
    treatment_course_number = models.IntegerField("通算治療回数", null=True, blank=True, help_text="course 内での通算セッション番号")
    
    # 転帰
    outcome_flags = models.JSONField("転帰", default=list, blank=True, help_text="['recovery', 'improvement'] など複数可")
    outcome_sequelae_text = models.CharField("後遺症詳細", max_length=300, blank=True)
    outcome_date = models.DateField("転帰日", null=True, blank=True)
    
    # その他
    special_notes = models.TextField("特記事項", blank=True)
    physician_comment = models.TextField("医師コメント", blank=True)
    
    # スナップショット（自動入力元を保存）
    prefilled_snapshot = models.JSONField("プリフィル元スナップショット", default=dict, blank=True, null=True)
    
    # 管理
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)
    
    class Meta:
        verbose_name = "有害事象報告書"
        verbose_name_plural = "有害事象報告書"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["session"]),
            models.Index(fields=["created_at"]),
        ]
    
    def __str__(self):
        return f"AdverseEventReport(session={self.session_id}) - {self.adverse_event_name or '未入力'}"