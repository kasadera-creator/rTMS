from django import forms
from .models import Patient

# 日付入力用の共通設定
class DateInput(forms.DateInput):
    input_type = 'date'

# ---------------------------------------------------------
# 1. 新規登録用フォーム (現場での簡易登録用)
# ---------------------------------------------------------
class PatientRegistrationForm(forms.ModelForm):
    class Meta:
        model = Patient
        # 登録時は基本的な項目＋予定日を入力できるようにする
        fields = [
            'card_id', 'name', 'birth_date', 'diagnosis', 
            'admission_date', 'mapping_date', 'first_treatment_date',
            'medical_history'
        ]
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 12345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 笠寺 太郎'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'diagnosis': forms.TextInput(attrs={'class': 'form-control', 'value': 'うつ病'}),
            'admission_date': DateInput(attrs={'class': 'form-control'}),
            'mapping_date': DateInput(attrs={'class': 'form-control'}),
            'first_treatment_date': DateInput(attrs={'class': 'form-control'}),
            # medical_historyは一旦隠すか、必要ならウィジェット設定を追加
        }
        labels = {
            'card_id': 'カルテ番号 (ID)',
            'name': '患者氏名',
            'birth_date': '生年月日',
            'diagnosis': '診断名',
            'admission_date': '入院予定日',
            'mapping_date': '位置決め日',
            'first_treatment_date': '初回治療日',
        }

    def clean_card_id(self):
        card_id = self.cleaned_data['card_id']
        if Patient.objects.filter(card_id=card_id).exists():
            raise forms.ValidationError("このカルテ番号は既に登録されています。")
        return card_id


# ---------------------------------------------------------
# 2. 編集用フォーム (基本情報のみ)
# ---------------------------------------------------------
class PatientBasicForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['card_id', 'name', 'birth_date', 'diagnosis', 'medical_history']
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'diagnosis': forms.TextInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'card_id': 'カルテ番号',
            'name': '氏名',
            'birth_date': '生年月日',
            'diagnosis': '診断名',
        }


# ---------------------------------------------------------
# 3. 編集用フォーム (スケジュール管理のみ)
# ---------------------------------------------------------
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