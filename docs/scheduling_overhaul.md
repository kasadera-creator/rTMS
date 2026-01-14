Scheduling overhaul — analysis

Date: 2026-01-14

Goal
----
Collect current implementation details for treatment scheduling, mapping and assessment, dashboard ToDo generation, and business-day/holiday handling so we can design a consolidated `schedule_tasks` service.

Findings (locations)
--------------------

- Core scheduling helpers: `rtms_app/services/rtms_schedule.py`
  - Functions: `generate_planned_dates`, `generate_treatment_dates`, `generate_mapping_dates`, `session_info_for_date`, `mapping_dates_from_planned`, `format_rtms_label`.
  - Business/holiday helpers: `is_closed(d, holidays)`, `next_open_day(d, holidays)`, `is_year_end_closed(d)`.
  - Behavior: treats Saturday(5)/Sunday(6) as closed; has year-end closure (Dec29–Jan3); supports passing `holidays` set.

- Usage sites (examples):
  - `rtms_app/views.py` imports and calls `generate_treatment_dates` and `generate_mapping_dates` in multiple places (clinical path generation, dashboard labels, etc.).
  - `views.generate_calendar_weeks()` uses `generate_treatment_dates` and `generate_mapping_dates` to build clinical-path weeks.

- Models:
  - `rtms_app/models.py`
    - `TreatmentSession` (holds `date` (datetime), `session_date` (date), `course_number`, and treatment params). `status` tracks planned/done/skipped.
    - `MappingSession` (holds `date`, `resting_mt`, `stimulation_site`, etc.). Currently `date` is the performed date.
    - `Assessment` (legacy): fields include `date` (DateField), `timing`, `scores`, plus `total_score_17/21`. No explicit `planned_date`/`performed_date` fields currently.

- Dashboard ToDo generation:
  - Implemented in `rtms_app/views.py::dashboard_view` (dynamic, per-request): builds `dashboard_tasks` lists by scanning `Patient` and related models and using `generate_treatment_dates` to detect sessions and assessments.
  - ToDo items are generated on-the-fly and not persisted to DB.

- Assessment/Mappings completion detection:
  - `Assessment` completion: present when an `Assessment` instance exists for patient/course_number/timing; code often uses `Assessment.objects.filter(...).exists()` to detect completion.
  - `MappingSession` completion: presence of a `MappingSession` object with `patient` and `date` is used.

- Business-day / holiday handling
  - `rtms_schedule.py` has `is_closed` and `next_open_day` and accepts a `holidays` set.
  - `views.py` defines `JP_HOLIDAYS` (hard-coded set of dates) and passes it into `generate_treatment_dates` / `generate_mapping_dates` in multiple places.

Immediate conclusions
---------------------

- Task generation is already centralized in `rtms_schedule.py` for treatment and mapping dates; it already supports holidays and year-end closure. Good foundation for consolidating task generation.
- Dashboard ToDo is currently generated dynamically (not stored). That aligns with the requested behavior of showing planned tasks until `performed_date` is set.
- `Assessment` model lacks explicit `performed_date`/`planned_date` fields — will need a DB migration to store `performed_date` (recommended). `MappingSession` has `date` (used as performed date); planned mapping dates are generated from `generate_mapping_dates`.

Next steps (proposed)
---------------------

1. Create `rtms_app/services/schedule_tasks.py` that wraps `rtms_schedule.py` functions and provides:
   - business-day helpers: `is_business_day`, `next_business_day`, `shift_to_next_business_day_if_needed`
   - `compute_task_definitions(patient)` returning mapping/assessment task definitions (name, timing, planned_date, window_end)
   - `compute_dashboard_tasks(patient, today)` producing ToDo items (planned_date <= today && not performed)

2. Add `performed_date` to `Assessment` model (DateField null/blank).

3. Refactor `views.py` (clinical path and dashboard) to call the new `schedule_tasks` APIs rather than inlining scheduling logic.

4. Implement `TreatmentSkip` model and UI/modal in `treatment_add.html` to record skip actions and call schedule regeneration (or apply postponement logic) as specified.

Notes / caveats
----------------
- Because many places call `generate_treatment_dates(...)` directly, replacement should be gradual: add the service and update consumers to call it; keep `rtms_schedule` as low-level utility.
- For holidays: current code uses `JP_HOLIDAYS` defined in `views.py`. We can keep that pattern (pass holiday set into service) or move `JP_HOLIDAYS` into `services/rtms_schedule.py` or a single config.
- Tests: need unit tests for `schedule_tasks` (edge cases: year-end, consecutive skips, is_all_case_survey branch for 4-week task).

Files touched (planned)
-----------------------
- New: `rtms_app/services/schedule_tasks.py` (service wrapper & task APIs)
- Edit: `rtms_app/models.py` (add `performed_date` to `Assessment`), plus migration
- Edit: `rtms_app/views.py` (dashboard_view, clinical path) to use new service
- New: `rtms_app/models.py` addition: `TreatmentSkip` model + migration
- Edit: `rtms_app/templates/rtms_app/treatment_add.html` add skip modal + JS to call backend
- New: backend endpoints to handle skip actions and schedule changes

I'll proceed to scaffold `rtms_app/services/schedule_tasks.py` with core helpers and create the analysis doc entry. After that I'll pause for your confirmation before changing models or views.
