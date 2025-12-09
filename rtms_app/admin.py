from django.contrib import admin
from django.db import models  # ★この行を追加してください
from django_jsonform.widgets import JSONFormWidget
from .models import Patient, TreatmentSession, Assessment

# --- チェックリストの定義 (ここを書き換えるだけで項目が増減します) ---

# 1. 問診・禁忌チェックリスト (ガイドライン参照)
MEDICAL_HISTORY_SCHEMA = {
    'type': 'object',
    'title': '問診・禁忌チェック',
    'properties': {
        'implants': {
            'type': 'boolean',
            'title': '【禁忌】金属・電子機器の体内留置 (ペースメーカー/人工内耳/クリップ等)',
            'helpText': '該当する場合は治療不可'
        },
        'epilepsy': {
            'type': 'boolean',
            'title': '【禁忌】てんかん・けいれん発作の既往',
        },
        'medication': {
            'type': 'string',
            'title': '服用中の薬剤',
            'widget': 'textarea',
            'default': '特になし'
        },
        'other_risks': {
            'type': 'array',
            'title': 'その他リスク因子',
            'items': {
                'type': 'string',
                'enum': [
                    '睡眠不足',
                    'アルコール多飲',
                    'カフェイン過剰摂取',
                    '頭部外傷の既往'
                ]
            },
            'widget': 'checkboxes',
            'uniqueItems': True
        }
    }
}

# 2. 治療後観察・有害事象リスト (ガイドライン参照)
ADVERSE_EVENTS_SCHEMA = {
    'type': 'object',
    'title': '治療後観察',
    'properties': {
        'status': {
            'type': 'string',
            'title': '状態',
            'enum': ['問題なし', '軽度の不快感あり', '有害事象あり'],
            'default': '問題なし'
        },
        'symptoms': {
            'type': 'array',
            'title': '症状詳細',
            'items': {
                'type': 'string',
                'enum': [
                    '頭皮痛(刺激痛)',
                    '刺激部位の不快感',
                    '歯痛',
                    '顔面のけいれん',
                    '頭痛',
                    'めまい',
                    '吐き気'
                ]
            },
            'widget': 'checkboxes',
            'uniqueItems': True
        },
        'note': {
            'type': 'string',
            'title': '自由記載メモ',
            'widget': 'textarea'
        }
    }
}

# --- 管理画面の設定 ---

class AssessmentInline(admin.TabularInline):
    model = Assessment
    extra = 0
    readonly_fields = ('note',)
    fields = ('date', 'timing', 'type', 'total_score', 'note')
    ordering = ('-date',)

class TreatmentSessionInline(admin.TabularInline):
    model = TreatmentSession
    extra = 0
    fields = ('date', 'motor_threshold', 'intensity', 'total_pulses', 'safety_sleep', 'safety_meds')
    ordering = ('-date',)

@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ('card_id', 'name', 'latest_hamd_display', 'improvement_display')
    search_fields = ('name', 'card_id')
    inlines = [AssessmentInline, TreatmentSessionInline]
    
    # ★ここでJSON入力をリッチなフォームに変換します
    formfield_overrides = {
        models.JSONField: {'widget': JSONFormWidget(schema=MEDICAL_HISTORY_SCHEMA)},
    }

    def latest_hamd_display(self, obj):
        return obj.get_latest_hamd()
    latest_hamd_display.short_description = "最新HAM-D"

    def improvement_display(self, obj):
        rate = obj.get_improvement_rate()
        return f"{rate}%" if rate is not None else "-"
    improvement_display.short_description = "改善率"

@admin.register(TreatmentSession)
class TreatmentSessionAdmin(admin.ModelAdmin):
    list_display = ('date', 'patient', 'motor_threshold', 'intensity')
    list_filter = ('date',)
    
    # ★治療記録側のJSONも変換
    formfield_overrides = {
        models.JSONField: {'widget': JSONFormWidget(schema=ADVERSE_EVENTS_SCHEMA)},
    }

admin.site.register(Assessment)