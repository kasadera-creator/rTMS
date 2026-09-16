from django import forms

from rtms_app.models import ResourcePool


class InpatientScheduleForm(forms.Form):
    admission_date = forms.DateField(label="入院予定日", widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    treatment_start_date = forms.DateField(label="rTMS開始日", widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    room_start_date = forms.DateField(label="個室使用開始日", widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    room_end_date = forms.DateField(label="個室使用終了日", required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    planned_discharge_date = forms.DateField(label="退院予定日", required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    estimated_discharge_date = forms.DateField(label="退院見込み日", required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    certainty = forms.ChoiceField(
        label="確定度",
        choices=(("confirmed", "確定"), ("provisional", "仮予定"), ("estimated", "推定")),
        initial="confirmed",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    resource_pool = forms.ModelChoiceField(
        label="個室資源",
        queryset=ResourcePool.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, resource_pool=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["resource_pool"].queryset = ResourcePool.objects.filter(
            resource_type="rTMS_private", is_active=True,
        ).order_by("name")
        if resource_pool is not None:
            self.initial["resource_pool"] = resource_pool

    def clean(self):
        cleaned = super().clean()
        admission = cleaned.get("admission_date")
        treatment = cleaned.get("treatment_start_date")
        room_start = cleaned.get("room_start_date")
        room_end = cleaned.get("room_end_date")
        if admission and treatment and treatment < admission:
            self.add_error("treatment_start_date", "rTMS開始日は入院予定日以降にしてください。")
        if room_start and room_end and room_end < room_start:
            self.add_error("room_end_date", "個室終了日は開始日以降にしてください。")
        return cleaned


class RtmSWaitlistEntryForm(forms.Form):
    preferred_start_from = forms.DateField(label="希望開始時期（早い方）", required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    preferred_start_to = forms.DateField(label="希望開始時期（遅い方）", required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    estimated_inpatient_days = forms.IntegerField(label="希望治療期間（日）", required=False, min_value=1, widget=forms.NumberInput(attrs={"class": "form-control"}))
    priority = forms.IntegerField(label="priority", min_value=0, initial=0, widget=forms.NumberInput(attrs={"class": "form-control"}))
    comment = forms.CharField(label="コメント", required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("preferred_start_from")
        end = cleaned.get("preferred_start_to")
        if start and end and end < start:
            self.add_error("preferred_start_to", "希望終了日は希望開始日以降にしてください。")
        return cleaned
