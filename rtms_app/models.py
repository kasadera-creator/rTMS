from django.db import models
from django.utils import timezone

class Patient(models.Model):
    """患者情報"""
    card_id = models.CharField("カルテ番号", max_length=20, unique=True)
    name = models.CharField("氏名", max_length=100)
    birth_date = models.DateField("生年月日")
    diagnosis = models.CharField("診断名", max_length=200, default="うつ病")
    
    # 既往歴やチェックリスト
    medical_history = models.JSONField("既往歴・禁忌チェック", default=dict, blank=True, null=True)
    created_at = models.DateTimeField("登録日", auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.card_id})"

    # ★追加: 最新のHAM-Dスコアを取得する機能
    def get_latest_hamd(self):
        latest = self.assessment_set.filter(type='HAM-D').order_by('-date').first()
        return latest.total_score if latest else None

    # ★追加: ベースライン（初回）との比較機能
    def get_improvement_rate(self):
        # 古い順に並べて最初をベースライン、最後を現在とする
        assessments = self.assessment_set.filter(type='HAM-D').order_by('date')
        if assessments.count() < 2:
            return None # 比較データなし
        
        baseline = assessments.first().total_score
        current = assessments.last().total_score
        
        if baseline == 0: return 0
        
        # 改善率 = (初期 - 現在) / 初期 * 100
        improvement = (baseline - current) / baseline * 100
        return round(improvement, 1)


class TreatmentSession(models.Model):
    """日々の治療実施記録 (Brainsway H1コイル)"""
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, verbose_name="患者")
    date = models.DateTimeField("実施日時", default=timezone.now)
    
    # 治療前安全確認
    safety_sleep = models.BooleanField("睡眠不足なし", default=True)
    safety_alcohol = models.BooleanField("アルコール・カフェイン過剰なし", default=True)
    safety_meds = models.BooleanField("服薬変更なし", default=True)
    
    # 治療パラメータ
    motor_threshold = models.IntegerField("MT(%)", help_text="安静時運動閾値")
    intensity = models.IntegerField("刺激強度(%)", default=120, help_text="通常120%")
    frequency = models.IntegerField("周波数(Hz)", default=18)
    duration_sec = models.IntegerField("刺激時間(秒)", default=2)
    interval_sec = models.IntegerField("刺激間隔(秒)", default=20)
    total_pulses = models.IntegerField("総パルス数", default=1980)
    
    # 実施後観察
    adverse_events = models.JSONField("有害事象・観察", default=dict, blank=True, null=True)
    
    def __str__(self):
        return f"{self.date.strftime('%Y-%m-%d')} - {self.patient.name}"


class Assessment(models.Model):
    """心理検査記録 (HAM-Dなど)"""
    ASSESSMENT_TYPES = [
        ('HAM-D', 'HAM-D (うつ病評価尺度)'),
        ('BDI-II', 'BDI-II (ベック抑うつ尺度)'),
        ('QIDS', 'QIDS (簡易抑うつ症状尺度)'),
    ]
    
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, verbose_name="患者")
    date = models.DateField("検査実施日", default=timezone.now)
    type = models.CharField("検査種別", max_length=20, choices=ASSESSMENT_TYPES, default='HAM-D')
    total_score = models.IntegerField("合計点")
    
    # 3週目・6週目のタグ付け用
    TIMING_CHOICES = [
        ('baseline', '治療前 (ベースライン)'),
        ('week3', '3週目 (継続判定)'),
        ('week6', '6週目 (最終評価)'),
        ('other', 'その他'),
    ]
    timing = models.CharField("評価タイミング", max_length=20, choices=TIMING_CHOICES, default='other')
    
    # 自動判定の結果を保存するフィールド（表示用）
    note = models.TextField("判定・特記事項", blank=True, help_text="自動判定結果などがここに入ります")

    def save(self, *args, **kwargs):
        # 保存前に自動判定ロジックを走らせる
        if self.type == 'HAM-D':
            self.generate_judgment()
        super().save(*args, **kwargs)

    def generate_judgment(self):
        # 1. 寛解判定 (Brainsway指針: 9点以下)
        if self.total_score <= 9:
            self.note = f"【寛解】スコア{self.total_score} (9点以下)。著しい改善です。"
            return

        # 2. 過去データと比較して反応率を計算
        baseline = Assessment.objects.filter(
            patient=self.patient, 
            type='HAM-D', 
            timing='baseline'
        ).first()
        
        if baseline and baseline.total_score > 0:
            improvement = (baseline.total_score - self.total_score) / baseline.total_score * 100
            result_str = f"ベースライン({baseline.total_score}点)から {improvement:.1f}% 改善。"
            
            if improvement >= 50:
                result_str += " 【反応あり(Response)】"
            elif self.timing == 'week3' and improvement < 20:
                result_str += " 【要検討】改善が乏しい可能性があります。"
            
            self.note = result_str
        else:
            if not self.note:
                self.note = "ベースラインとの比較不可"

    def __str__(self):
        return f"{self.date} - {self.get_type_display()}: {self.total_score}点"