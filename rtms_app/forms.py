from django import forms
from .models import Patient

# 日付入力用の共通ウィジェット設定
class DateInput(forms.DateInput):
    input_type = 'date'

# 1. 基本情報編集フォーム
class PatientBasicForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['card_id', 'name', 'birth_date', 'diagnosis', 'medical_history']
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'diagnosis': forms.TextInput(attrs={'class': 'form-control'}),
            # JSONFieldは別途対応するか、簡易テキストエリアとして一旦表示
        }
        labels = {
            'card_id': 'カルテ番号',
            'name': '氏名',
            'birth_date': '生年月日',
            'diagnosis': '診断名',
        }

# 2. スケジュール管理フォーム
class PatientScheduleForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['admission_date', 'mapping_date', 'first_treatment_date']
        widgets = {
            'admission_date': DateInput(attrs={'class': 'form-control'}),
            'mapping_date': DateInput(attrs={'class': 'form-control'}),
            'first_treatment_date': DateInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'admission_date': '入院予定日',
            'mapping_date': '位置決め日',
            'first_treatment_date': '初回治療日',
        }