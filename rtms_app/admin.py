from django.contrib import admin
from django.db import models
from django_jsonform.widgets import JSONFormWidget
from .models import Patient, TreatmentSession, Assessment, ConsentDocument

# --- 1. 適正に関する質問票 (初診時) ---
QUESTIONNAIRE_SCHEMA = {
    'type': 'object',
    'title': 'rTMS 適正質問票',
    'properties': {
        'implants': {
            'type': 'string',
            'title': '1. 頭部・体内に金属や電子機器が入っていますか？',
            'enum': ['いいえ', 'はい'],
            'widget': 'radio',
            'helpText': 'ペースメーカー、人工内耳、脳動脈クリップ、インプラント等'
        },
        'epilepsy': {
            'type': 'string',
            'title': '2. けいれん発作（てんかん）の既往、または脳波異常はありますか？',
            'enum': ['いいえ', 'はい'],
            'widget': 'radio'
        },
        'brain_disease': {
            'type': 'string',
            'title': '3. 脳卒中、脳腫瘍、頭部外傷などの脳の病気はありますか？',
            'enum': ['いいえ', 'はい'],
            'widget': 'radio'
        },
        'pregnancy': {
            'type': 'string',
            'title': '4. 現在妊娠している、またはその可能性はありますか？',
            'enum': ['いいえ', 'はい'],
            'widget': 'radio'
        },
        'medication_change': {
            'type': 'string',
            'title': '5. 最近お薬の変更はありましたか？',
            'enum': ['いいえ', 'はい'],
            'widget': 'radio'
        },
        'sleep_deprivation': {
            'type': 'string',
            'title': '6. 昨夜は十分睡眠がとれましたか？（睡眠不足ではないですか？）',
            'enum': ['はい (十分とれた)', 'いいえ (睡眠不足)'],
            'widget': 'radio'
        },
        'alcohol_caffeine': {
            'type': 'string',
            'title': '7. アルコールやカフェインを過剰に摂取していませんか？',
            'enum': ['いいえ', 'はい'],
            'widget': 'radio'
        },
        'doctor_check': {
            'type': 'boolean',
            'title': '医師による確認済',
            'helpText': '上記項目を確認し、治療適応と判断しました。'
        }
    }
}

# --- 2. 副作用チェック表 (治療毎) ---
SIDE_EFFECT_SCHEMA = {
    'type': 'object',
    'title': '副作用チェック表',
    'properties': {
        'symptoms': {
            'type': 'array',
            'title': '自覚症状の有無',
            'items': {
                'type': 'object',
                'properties': {
                    'name': {
                        'type': 'string',
                        'title': '症状',
                        'enum': [
                            '頭皮痛（刺激痛）', '刺激部位の不快感', '歯痛', 
                            '顔面のけいれん', '頭痛', 'めまい', '吐き気', 
                            '耳鳴り', '聴力低下', '不安感・焦燥感', 'その他'
                        ]
                    },
                    'severity': {
                        'type': 'string',
                        'title': '程度',
                        'enum': ['なし', '軽度', '中等度', '重度'],
                        'widget': 'radio',
                        'default': 'なし'
                    }
                }
            }
        },
        'seizure_sign': {
            'type': 'boolean',
            'title': 'けいれん発作の兆候なし',
            'default': True
        },
        'note': {
            'type': 'string',
            'title': '特記事項',
            'widget': 'textarea'
        }
    }
}

# --- Admin設定 ---
@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ('card_id', 'name', 'diagnosis')
    search_fields = ('name', 'card_id')
    
    # JSON入力フォームの適用
    # formfield_overrides = { ... } # 前回の定義があれば維持

    # ★ここを修正（エラーの原因箇所）
    fields = (
        ('card_id', 'name'), 
        ('birth_date', 'gender'),
        ('diagnosis', 'attending_physician'),
        ('referral_source'),
        # medical_history を削除し、詳細項目へ
        'life_history', 'past_history', 'present_illness', 'medication_history',
        # スケジュール
        ('admission_date', 'mapping_date', 'first_treatment_date'),
        'mapping_notes', # 追加
        'questionnaire_data'
    )

@admin.register(TreatmentSession)
class TreatmentSessionAdmin(admin.ModelAdmin):
    list_display = ('date', 'patient', 'performer')
    list_filter = ('date',)
    # formfield_overrides ...

admin.site.register(Assessment)


@admin.register(ConsentDocument)
class ConsentDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "uploaded_at", "file")


