## Manual checklist
- Change session date in safety/implementation section → Save → redirect includes focus=YYYY-MM-DD
- Calendar jumps/highlights the focused date; URL focus param is removed after applying
- Changing to a different month still jumps (redirects to correct month)
- Double-submit is prevented (button disabled / no rollback)
- HAM-D: Q18 shows B only; improvement >=20% shows "反応"; <=7 shows "寛解"
- Admission procedure completion sets patient.status=inpatient and appears in inpatient filter

Notes:
- The focus handler is applied to `calendar_month.html` and `patient_clinical_path.html` for now.
- Apply to other calendar templates later when those views are in active use.
