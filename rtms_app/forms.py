from django import forms
from django.contrib.auth.models import User
from .models import Patient, MappingSession, TreatmentSession, Assessment
import datetime

class DateInput(forms.DateInput):
    input_type = 'date'

class TimeInput(forms.TimeInput):
    input_type = 'time'

class PhysicianChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        name = f"{obj.last_name} {obj.first_name}" if obj.last_name else obj.username
        return f"{name}"

# --- 1. 新規登録フォーム ---
class PatientRegistrationForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['card_id', 'name', 'birth_date', 'gender', 'referral_source', 'referral_doctor']
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 12345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 笠寺 太郎'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            'referral_source': forms.TextInput(attrs={'class': 'form-control', 'list': 'referral-options', 'placeholder': '医療機関名 (任意)'}),
            'referral_doctor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '医師名 (任意)'}),
        }


class PatientBasicEditForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['card_id', 'name', 'birth_date', 'gender', 'referral_source', 'referral_doctor']
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            'referral_source': forms.TextInput(attrs={'class': 'form-control', 'list': 'referral-options', 'placeholder': '医療機関名 (任意)'}),
            'referral_doctor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '医師名 (任意)'}),
        }

# --- 2. 初診フォーム (修正: バリデーション追加) ---
class PatientFirstVisitForm(forms.ModelForm):
    attending_physician = PhysicianChoiceField(
        queryset=User.objects.filter(groups__name='医師'),
        label="担当医", required=False, widget=forms.Select(attrs={'class': 'form-select'})
    )
    
    diagnosis = forms.CharField(widget=forms.HiddenInput(), required=False)

    # --- 全例調査対象 ---
    is_all_case_survey = forms.BooleanField(label='全例調査対象', required=False, initial=False, widget=forms.CheckboxInput(attrs={'class':'form-check-input','id':'id_is_all_case_survey'}))

    # --- 原疾患（うつ病）推定発症年月 ---
    estimated_onset_year = forms.IntegerField(
        label='原疾患（うつ病）推定発症年', required=False, min_value=1800, max_value=2100,
        widget=forms.NumberInput(attrs={
            'class':'form-control form-control-sm',
            'id':'id_estimated_onset_year',
            'style':'width:6rem;display:inline-block;'
        })
    )
    estimated_onset_month = forms.IntegerField(
        label='原疾患（うつ病）推定発症月', required=False, min_value=1, max_value=12,
        widget=forms.NumberInput(attrs={
            'class':'form-control form-control-sm',
            'id':'id_estimated_onset_month',
            'style':'width:4rem;display:inline-block;'
        })
    )

    # --- 診断名（既往精神疾患） ---
    HAS_PSY_CHOICES = [
        ('yes', '有'),
        ('no', '無'),
        ('unknown', '不明'),
    ]
    has_other_psychiatric_history = forms.ChoiceField(label='診断名（今回 rTMS の適応となるうつ病以外の精神疾患の既往）', choices=HAS_PSY_CHOICES, widget=forms.RadioSelect(attrs={'class':'form-check-input'}), initial='no', required=False)

    PSY_HISTORY_CHOICES = [
        ('F30', '躁病エピソード（F30）'),
        ('F31', '双極性感情障害（F31）'),
        ('F32', 'うつ病エピソード（F32）'),
        ('F33', '反復性うつ病性障害（F33）'),
        ('F34', '持続性気分（感情）障害（F34）'),
        ('F38', '他の気分（感情）障害（F38）'),
        ('F20', '統合失調症（F20）'),
        ('OCD', '強迫症'),
        ('SAD', '社交不安症'),
        ('Panic', 'パニック症'),
        ('Agoraphobia', '広場恐怖症'),
        ('GAD', '全般性不安症'),
        ('PTSD', '心的外傷後ストレス症'),
        ('ID', '知的発達症'),
        ('ASD', '自閉スペクトラム症'),
        ('ADHD', '注意欠如多動症'),
        ('MCI', '軽度認知障害（MCI）'),
        ('Alzheimer', 'アルツハイマー型認知症'),
        ('DLB', 'レビー小体型認知症'),
        ('Parkinson', 'パーキンソン病'),
        ('Other', 'その他'),
    ]
    psychiatric_history = forms.MultipleChoiceField(label='病名（有の場合に選択）', choices=PSY_HISTORY_CHOICES, required=False, widget=forms.CheckboxSelectMultiple(attrs={'class':'form-check-input'}))
    psychiatric_history_other_text = forms.CharField(label='その他（自由記載）', required=False, widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'id':'id_psychiatric_history_other_text'}))

    class Meta:
        model = Patient
        fields = [
            'card_id', 'name', 'birth_date', 'gender', 'attending_physician', 
            'referral_source', 'referral_doctor',
            'chief_complaint', 'diagnosis', 
                'life_history', 'past_history', 'present_illness', 'medication_history', 
                    'is_all_case_survey', 'estimated_onset_year', 'estimated_onset_month',
                    'weight_kg', 'is_weight_unknown',
            'has_other_psychiatric_history', 'psychiatric_history', 'psychiatric_history_other_text',
            'admission_date', 'first_treatment_date'
        ]
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            'referral_source': forms.TextInput(attrs={'class': 'form-control', 'list': 'referral-options', 'placeholder': '医療機関名'}),
            'referral_doctor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '医師名 (姓のみ、またはフルネーム)'}),
            'chief_complaint': forms.TextInput(attrs={'class': 'form-control'}),
            'life_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'past_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'present_illness': forms.Textarea(attrs={'class': 'form-control', 'rows': 7}),
            'medication_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 6}),
            'dementia_detail': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': '', 'style': 'display: inline-block; width: auto; margin-left: 10px;'}),
            'admission_date': DateInput(attrs={'class': 'form-control'}),
            'first_treatment_date': DateInput(attrs={'class': 'form-control'}),
        }

    # weight and unknown fields (form-only rendering control)
    weight_kg = forms.DecimalField(label='体重(kg)', required=False, min_value=0, max_digits=4, decimal_places=1, widget=forms.NumberInput(attrs={'class':'form-control','step':'1','id':'id_weight_kg','placeholder':'例: 65'}))
    is_weight_unknown = forms.BooleanField(label='体重不明', required=False, widget=forms.CheckboxInput(attrs={'class':'form-check-input','id':'id_is_weight_unknown'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default_medication_text = ("抗うつ薬：\n抗精神病薬：\n気分安定薬：\n抗不安薬：\n睡眠薬：\nその他：")
        if not self.instance.medication_history:
            self.instance.medication_history = default_medication_text
            self.initial['medication_history'] = default_medication_text
    
    # ★追加: 保存時のバリデーション
    def clean(self):
        cleaned_data = super().clean()
        
        # 必須チェックを行う項目
        required_fields = {
            'admission_date': '入院予定日',
            'first_treatment_date': '初回治療日',
            'attending_physician': '今後の担当医',
        }
        
        errors = []
        for field_name, label in required_fields.items():
            if not cleaned_data.get(field_name):
                self.add_error(field_name, f"{label}を入力してください。")
        # If psychiatric history is not '有', clear any selected disease values
        psy_flag = cleaned_data.get('has_other_psychiatric_history')
        if psy_flag != 'yes':
            cleaned_data['psychiatric_history'] = []
            cleaned_data['psychiatric_history_other_text'] = ''

        # Weight: if unknown checked, clear numeric value; otherwise ensure consistency
        weight_unknown = cleaned_data.get('is_weight_unknown')
        weight_val = cleaned_data.get('weight_kg')
        if weight_unknown:
            cleaned_data['weight_kg'] = None
        else:
            if weight_val is not None:
                cleaned_data['is_weight_unknown'] = False

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Transfer form-only fields into model fields
        instance.is_all_case_survey = bool(self.cleaned_data.get('is_all_case_survey'))
        instance.estimated_onset_year = self.cleaned_data.get('estimated_onset_year')
        instance.estimated_onset_month = self.cleaned_data.get('estimated_onset_month')
        instance.has_other_psychiatric_history = self.cleaned_data.get('has_other_psychiatric_history') or 'no'

        # psychiatric_history: store as list (or empty list)
        psy_list = self.cleaned_data.get('psychiatric_history') or []
        instance.psychiatric_history = list(psy_list)
        instance.psychiatric_history_other_text = self.cleaned_data.get('psychiatric_history_other_text') or ''

        # Save weight fields into model
        instance.is_weight_unknown = bool(self.cleaned_data.get('is_weight_unknown'))
        w = self.cleaned_data.get('weight_kg')
        instance.weight_kg = None if instance.is_weight_unknown else (w if w is not None else None)

        if commit:
            instance.save()
        return instance

# --- 3. 位置決めフォーム ---
class MappingForm(forms.ModelForm):
    class Meta:
        model = MappingSession
        fields = [
            'date', 'week_number', 'resting_mt',
            'helmet_position_a_x', 'helmet_position_a_y',
            'helmet_position_b_x', 'helmet_position_b_y',
            'notes'
        ]
        widgets = {
            'date': DateInput(attrs={'class': 'form-control'}),
            'week_number': forms.Select(attrs={'class': 'form-select'}),
            'resting_mt': forms.NumberInput(attrs={'class': 'form-control', 'required': True, 'min': 0, 'step': '1', 'value': '60'}),
            'helmet_position_a_x': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5', 'value': '3', 'placeholder': 'X座標'}),
            'helmet_position_a_y': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5', 'value': '1', 'placeholder': 'Y座標'}),
            'helmet_position_b_x': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5', 'value': '9', 'placeholder': 'X座標'}),
            'helmet_position_b_y': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5', 'value': '1', 'placeholder': 'Y座標'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2})
        }

# --- 4. 治療実施フォーム ---
class TreatmentForm(forms.ModelForm):
    # ★修正: 日付と時間を分離
    treatment_date = forms.DateField(label='実施日', widget=DateInput(attrs={'class': 'form-control', 'required': True}))
    treatment_time = forms.TimeField(label='開始時間', widget=TimeInput(attrs={'class': 'form-control', 'required': True}))

    class Meta:
        model = TreatmentSession
        fields = [
            'safety_sleep', 'safety_alcohol', 'safety_meds',
            'coil_type', 'target_site',
            'mt_percent',
            'train_seconds', 'frequency_hz', 'intertrain_seconds',
            'train_count', 'total_pulses',
            'treatment_notes',
        ]
        widgets = {
            'coil_type': forms.TextInput(attrs={'class': 'form-control', 'required': True, 'value': 'BrainsWay H1'}),
            'target_site': forms.TextInput(attrs={'class': 'form-control', 'required': True, 'value': '左DLPFC'}),
            'mt_percent': forms.NumberInput(attrs={'class': 'form-control', 'required': True, 'min': 0}),
            'train_seconds': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1', 'required': True, 'value': 2}),
            'frequency_hz': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1', 'required': True, 'value': 18}),
            'intertrain_seconds': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1', 'required': True, 'value': 20}),
            'train_count': forms.NumberInput(attrs={'class': 'form-control', 'required': True, 'min': 1, 'value': 55}),
            'total_pulses': forms.NumberInput(attrs={'class': 'form-control', 'required': True, 'min': 0, 'value': 1980}),
            'treatment_notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

# --- 5. 入院手続きフォーム ---
class AdmissionProcedureForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['admission_type', 'is_admission_procedure_done']
        widgets = {'admission_type': forms.RadioSelect(attrs={'class': 'form-check-input'})}