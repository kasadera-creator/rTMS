from django.contrib import admin
from .models import Patient, TreatmentSession, Assessment

# 患者画面の中に、検査履歴を埋め込む設定
class AssessmentInline(admin.TabularInline):
    model = Assessment
    extra = 0 # 空欄を勝手に出さない
    readonly_fields = ('note',) # 自動判定結果は書き換え不可にする
    fields = ('date', 'timing', 'type', 'total_score', 'note')
    ordering = ('-date',)

# 患者画面の中に、日々の治療記録を埋め込む設定
class TreatmentSessionInline(admin.TabularInline):
    model = TreatmentSession
    extra = 0
    fields = ('date', 'motor_threshold', 'intensity', 'total_pulses', 'safety_sleep', 'safety_meds')
    ordering = ('-date',)

# 患者管理画面のメイン設定
@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ('card_id', 'name', 'latest_hamd_display', 'improvement_display')
    search_fields = ('name', 'card_id')
    inlines = [AssessmentInline, TreatmentSessionInline]

    # リスト画面に最新HAM-Dを表示
    def latest_hamd_display(self, obj):
        return obj.get_latest_hamd()
    latest_hamd_display.short_description = "最新HAM-D"

    # リスト画面に改善率を表示
    def improvement_display(self, obj):
        rate = obj.get_improvement_rate()
        if rate is not None:
            return f"{rate}%"
        return "-"
    improvement_display.short_description = "改善率"

# 個別メニューとしても登録
admin.site.register(TreatmentSession)
admin.site.register(Assessment)