# Print Inventory

This document lists all printing endpoints, views, templates and rules.

| content_label | URL (example) | url_name | view | template | date rule | patient_id / course_no source |
|---|---:|---|---|---|---|---|
| 入院時サマリー | /app/patient/123/print/admission/ | print:patient_print_admission | `print_views.patient_print_admission` | `rtms_app/print/admission_summary.html` | uses patient's estimated end date, not explicit | patient from URL, course_number from `patient.course_number` |
| 臨床経過表 | /app/patient/123/print/path/ | print:print_clinical_path | `print_views.print_clinical_path` | `rtms_app/print/path.html` | calendar range (generated from patient mapping/treatment dates) | patient from URL |
| 退院時サマリー（bundle） | /app/patient/123/print/bundle/?docs=discharge | print:patient_print_bundle | `print_views.patient_print_bundle` | `rtms_app/print/bundle.html` (includes `discharge_summary.html`) | uses provided `docs` query; dates inside each doc as defined | patient from URL |
| 紹介状 | /app/patient/123/print/referral/ | print:patient_print_referral | `print_views.patient_print_referral` | `rtms_app/print/referral.html` | uses patient data; date = today or return_to | patient from URL |
| rTMS問診票 | /app/patient/123/print/suitability/ | print:patient_print_suitability | `print_views.patient_print_suitability` | `rtms_app/print/suitability_questionnaire.html` | uses baseline assessment or patient data | patient from URL |
| 副作用チェック表（治療実施記録票） | /app/patient/123/print/side_effect/456/ | print:print_side_effect_check | `print_views.print_side_effect_check` | `rtms_app/print/side_effect_check.html` | session-specific: uses `session.date` or session info | patient from URL, session id in path, course_no from session or patient |
| 有害事象報告書（DB版） | /app/session/456/adverse-event/print/ | (adverse_event_report_print) | `views.adverse_event_report_print` (separate) | `rtms_app/print/adverse_event_report_db.html` | session-specific, uses adverse event record date | session_id in path, patient pulled from session |
| 臨床テーブル / HAMD 等 | /app/patient/123/print/hamd_detail/  (various) | (subset) | `print_views.*` or other views | `rtms_app/print/_hamd_trend_print_compact.html`, `hamd_detail.html` | varies | patient from URL |

Notes:
- Print routes are registered in `rtms_app/print_urls.py` and also mounted under `/app/print/` in `config/urls.py`.
- Some print endpoints (e.g. adverse-event print) are implemented outside the `print` namespace (see `ADVERSE_EVENT_IMPLEMENTATION.md`).
- Filename generation and PDF-saving behavior are centralized in `rtms_app/services/print_service.py` (to be added).

Next: implement shared toolbar and PDF-saving helpers, then wrap the templates listed above with `#print-root` and include the toolbar.
