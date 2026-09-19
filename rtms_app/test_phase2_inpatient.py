from datetime import date, datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from rtms_app.models import (
    InpatientPlan,
    Patient,
    Assessment,
    AssessmentRecord,
    ResourceAssignment,
    ResourcePool,
    RtmSAdmissionCapacity,
    RtmSWaitlistEntry,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services.inpatient_planning import confirm_inpatient_schedule
from rtms_app.services.resource_capacity import (
    CapacityConflict,
    capacity_snapshot,
    earliest_admission_forecast,
)
from rtms_app.services.rtms_inpatient_calendar import (
    build_inpatient_calendar,
    build_inpatient_calendar_range,
)
from rtms_app.services.waitlist import (
    WaitlistAlreadyRegisteredError,
    register_waitlist_entry,
)
from rtms_app.views import generate_calendar_weeks


class Phase2InpatientTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="phase2-user", password="pw")
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id="82001", name="Phase Two", birth_date=date(1980, 1, 1),
        )
        self.course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        self.course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)
        self.pool = ResourcePool.objects.create(
            code="PHASE2_PRIVATE", name="rTMS個室", resource_type="rTMS_private",
            physical_capacity=5, operational_target=3,
        )
        self.capacity = RtmSAdmissionCapacity.objects.create(
            capacity=3, valid_from=date(2026, 10, 1), valid_to=date(2026, 10, 31),
        )

    def test_independent_capacity_values_and_operational_warning(self):
        self.assertEqual(self.pool.physical_capacity, 5)
        self.assertEqual(self.pool.operational_target, 3)
        self.assertEqual(self.capacity.capacity, 3)
        for index in range(3):
            course = TreatmentCourse.objects.create(
                patient=Patient.objects.create(
                    card_id=f"8200{index + 2}", name=f"Room {index}", birth_date=date(1980, 1, 1),
                ),
                course_number=1,
            )
            ResourceAssignment.objects.create(
                treatment_course=course, resource_pool=self.pool,
                planned_start_date=date(2026, 10, 5), planned_end_date=date(2026, 10, 15),
            )
        snapshot = capacity_snapshot(self.pool, date(2026, 10, 5), date(2026, 10, 15))
        self.assertEqual(snapshot["planned_count"], 3)
        self.assertTrue(snapshot["physical_capacity_available"])
        self.assertTrue(snapshot["operational_target_warning"])

    def test_schedule_confirmation_creates_plan_and_assignment_without_sessions(self):
        result = confirm_inpatient_schedule(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5),
            room_end_date=date(2026, 10, 15),
            planned_discharge_date=date(2026, 10, 20),
            user=self.user,
        )
        self.assertEqual(result["plan"].status, "scheduled")
        self.assertEqual(result["assignment"].planned_start_date, date(2026, 10, 5))
        self.assertEqual(ResourceAssignment.objects.count(), 1)
        self.assertEqual(TreatmentSession.objects.count(), 0)
        self.course_one.refresh_from_db()
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 6))

    def test_admission_capacity_rejects_second_new_admission_when_limit_is_one(self):
        self.capacity.capacity = 1
        self.capacity.save(update_fields=["capacity", "updated_at"])
        confirm_inpatient_schedule(
            treatment_course=self.course_one, resource_pool=self.pool,
            admission_date=date(2026, 10, 5), treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5), room_end_date=date(2026, 10, 15), user=self.user,
        )
        with self.assertRaises(CapacityConflict):
            confirm_inpatient_schedule(
                treatment_course=self.course_two, resource_pool=self.pool,
                admission_date=date(2026, 10, 5), treatment_start_date=date(2026, 10, 6),
                room_start_date=date(2026, 10, 5), room_end_date=date(2026, 10, 15), user=self.user,
            )
        self.assertEqual(ResourceAssignment.objects.count(), 1)

    def test_waitlist_registration_creates_no_assignment_and_keeps_history(self):
        first = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user,
            status="waiting", preferred_start_note="10月中旬希望", priority=1,
        )
        second = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="withdrawn", preferred_start_note="11月以降希望", priority=2,
        )
        self.assertEqual(self.course_one.rtms_waitlist_entries.count(), 2)
        self.assertIsNone(first.inpatient_plan_id)
        self.assertIsNone(second.inpatient_plan_id)
        self.assertEqual(ResourceAssignment.objects.count(), 0)

    def test_waitlist_registration_rejects_existing_reserved_statuses(self):
        for index, status in enumerate(("requested", "waiting", "provisional", "scheduled"), start=10):
            with self.subTest(status=status):
                course = TreatmentCourse.objects.create(
                    patient=self.patient, course_number=index,
                )
                RtmSWaitlistEntry.objects.create(treatment_course=course, status=status)
                with self.assertRaises(WaitlistAlreadyRegisteredError):
                    register_waitlist_entry(
                        treatment_course=course, user=self.user, status="waiting",
                    )

    def test_waitlist_registration_is_scoped_to_treatment_course(self):
        first = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user, status="waiting",
        )
        second = register_waitlist_entry(
            treatment_course=self.course_two, user=self.user, status="waiting",
        )
        self.assertEqual(first.treatment_course_id, self.course_one.pk)
        self.assertEqual(second.treatment_course_id, self.course_two.pk)

    def test_waitlist_registration_allows_new_entry_after_cancelled_or_withdrawn(self):
        for index, status in enumerate(("cancelled", "withdrawn"), start=20):
            with self.subTest(status=status):
                course = TreatmentCourse.objects.create(
                    patient=self.patient, course_number=index,
                )
                RtmSWaitlistEntry.objects.create(treatment_course=course, status=status)
                entry = register_waitlist_entry(
                    treatment_course=course, user=self.user, status="waiting",
                )
                self.assertEqual(entry.status, "waiting")

    def test_waitlist_post_shows_duplicate_error(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one, status="waiting",
        )
        response = self.client.post(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]),
            {"course_number": 1, "priority": 0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "このCourseはすでにrTMS待機リストに登録されています。")
        self.assertEqual(self.course_one.rtms_waitlist_entries.count(), 1)

    def test_waitlist_database_constraint_rejects_reserved_duplicate(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one, status="waiting",
        )
        with self.assertRaises(IntegrityError):
            RtmSWaitlistEntry.objects.create(
                treatment_course=self.course_one, status="scheduled",
            )

    def test_waitlist_entry_becomes_scheduled_only_with_explicit_confirmation(self):
        entry = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user, status="waiting",
        )
        confirm_inpatient_schedule(
            treatment_course=self.course_one, resource_pool=self.pool,
            admission_date=date(2026, 10, 5), treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5), room_end_date=date(2026, 10, 15),
            user=self.user, waitlist_entry=entry,
        )
        entry.refresh_from_db()
        self.assertEqual(entry.status, "scheduled")
        self.assertIsNotNone(entry.inpatient_plan_id)

    def test_calendar_is_course_scoped_and_preserves_three_timelines(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one, status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            planned_discharge_date=date(2026, 10, 20),
        )
        ResourceAssignment.objects.create(
            treatment_course=self.course_one, resource_pool=self.pool,
            planned_start_date=date(2026, 10, 5), planned_end_date=date(2026, 10, 10),
        )
        TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            session_date=date(2026, 10, 6), status="planned",
        )
        TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            session_date=date(2026, 10, 6), status="planned",
        )
        context = build_inpatient_calendar(year=2026, month=10, treatment_course=self.course_one)
        selected = next(day for day in context["days"] if day["date"] == date(2026, 10, 6))
        self.assertEqual(selected["inpatient_count"], 1)
        self.assertEqual(selected["rtms_count"], 1)
        self.assertEqual(selected["private_room_count"], 1)
        self.assertEqual(plan.treatment_course_id, self.course_one.pk)

    def test_calendar_rows_keep_three_timelines_separate_by_course(self):
        first_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 10, 2),
            planned_discharge_date=date(2026, 10, 8),
        )
        second_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="provisional",
            planned_admission_date=date(2026, 10, 12),
            estimated_discharge_date=date(2026, 10, 18),
        )
        first_assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 3),
            planned_end_date=date(2026, 10, 7),
        )
        second_assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_two,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 13),
            planned_end_date=date(2026, 10, 17),
        )
        first_session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            session_date=date(2026, 10, 4),
            status="done",
        )
        second_session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 10, 14),
            status="planned",
        )

        context = build_inpatient_calendar(year=2026, month=10)
        rows = {row["treatment_course_id"]: row for row in context["calendar_rows"]}

        first_row = rows[self.course_one.pk]
        second_row = rows[self.course_two.pk]
        self.assertEqual(first_row["patient_id"], self.patient.pk)
        self.assertEqual(first_row["course_number"], 1)
        self.assertEqual(first_row["timelines"]["inpatient"][0]["start"], first_plan.planned_admission_date)
        self.assertEqual(first_row["timelines"]["inpatient"][0]["end"], first_plan.planned_discharge_date)
        self.assertEqual(first_row["timelines"]["inpatient"][0]["end_kind"], "planned")
        self.assertEqual(first_row["timelines"]["private_room"][0]["resource_pool"], self.pool)
        self.assertEqual(first_row["timelines"]["private_room"][0]["start"], first_assignment.planned_start_date)
        self.assertEqual(first_row["timelines"]["rtms"][0]["session_id"], first_session.pk)
        self.assertEqual(first_row["timelines"]["rtms"][0]["status"], "done")
        self.assertEqual(second_row["course_number"], 2)
        self.assertEqual(second_row["timelines"]["inpatient"][0]["status"], second_plan.status)
        self.assertEqual(second_row["timelines"]["private_room"][0]["start"], second_assignment.planned_start_date)
        self.assertEqual(second_row["timelines"]["rtms"][0]["session_id"], second_session.pk)
        self.assertNotEqual(
            first_row["timelines"]["rtms"][0]["session_id"],
            second_row["timelines"]["rtms"][0]["session_id"],
        )

    def test_weekly_calendar_covers_eighteen_monday_weeks_and_preserves_course_data(self):
        first_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 18),
            planned_discharge_date=date(2026, 9, 30),
        )
        second_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="provisional",
            planned_admission_date=date(2026, 10, 9),
            planned_discharge_date=date(2026, 10, 20),
        )
        first_assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 9, 19),
            planned_end_date=date(2026, 9, 25),
        )
        first_sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                session_date=session_date,
                status=status,
            )
            for session_date, status in (
                (date(2026, 9, 21), "planned"),
                (date(2026, 9, 22), "done"),
            )
        ]
        second_session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 10, 12),
            status="skipped",
        )

        context = build_inpatient_calendar_range(
            start_date=date(2026, 9, 16),
            today=date(2026, 9, 16),
        )

        self.assertEqual(context["week_count"], 18)
        self.assertEqual(context["weeks"][0]["start"], date(2026, 9, 14))
        self.assertEqual(context["weeks"][0]["end"], date(2026, 9, 20))
        self.assertTrue(context["weeks"][0]["is_current"])
        self.assertEqual(context["month_spans"][0]["label"], "2026年9月")
        self.assertEqual(context["month_spans"][1]["label"], "2026年10月")
        rows = {row["treatment_course_id"]: row for row in context["weekly_calendar_rows"]}
        first_row = rows[self.course_one.pk]
        second_row = rows[self.course_two.pk]
        self.assertEqual(first_row["timelines"]["inpatient"][0]["start"], first_plan.planned_admission_date)
        self.assertEqual(first_row["timelines"]["private_room"][0]["start"], first_assignment.planned_start_date)
        self.assertEqual(
            {item["session_id"] for item in first_row["timelines"]["rtms"]},
            {session.pk for session in first_sessions},
        )
        self.assertEqual(len(first_row["timelines"]["rtms_period"]), 1)
        period = first_row["timelines"]["rtms_period"][0]
        self.assertEqual(period["start"], date(2026, 9, 21))
        self.assertEqual(period["end"], date(2026, 9, 22))
        self.assertEqual(period["session_count"], 2)
        self.assertEqual(period["planned_count"], 1)
        self.assertEqual(period["done_count"], 1)
        self.assertEqual(second_row["timelines"]["inpatient"][0]["start"], second_plan.planned_admission_date)
        self.assertEqual(second_row["timelines"]["rtms"][0]["session_id"], second_session.pk)
        self.assertEqual(second_row["timelines"]["rtms_period"][0]["skipped_count"], 1)
        self.assertEqual(context["weekly_summary"]["peak_rtms_courses"], 1)
        self.assertIs(context["weeks"], context["weekly_load"])
        self.assertEqual(
            [item["week"] for item in context["weekly_metrics"][0]["weeks"]],
            context["weeks"],
        )

    def test_calendar_course_adjustment_updates_course_and_inpatient_plan(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            priority=1,
        )
        response = self.client.post(
            reverse("rtms_app:inpatient_calendar"),
            {
                "action": "save_calendar_course",
                "treatment_course_id": str(self.course_one.pk),
                "admission_date": "2026-10-05",
                "first_treatment_date": "2026-10-06",
                "private_room_planned": "on",
                "is_all_case_survey": "on",
                "year": "2026",
                "month": "10",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.course_one.refresh_from_db()
        self.course_two.refresh_from_db()
        self.assertEqual(self.course_one.admission_date, date(2026, 10, 5))
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 6))
        self.assertTrue(self.course_one.private_room_planned)
        self.assertTrue(self.course_one.is_all_case_survey)
        self.assertFalse(self.course_two.private_room_planned)
        self.assertFalse(self.course_two.is_all_case_survey)
        plan = InpatientPlan.objects.get(treatment_course=self.course_one)
        self.assertEqual(plan.planned_admission_date, date(2026, 10, 5))
        self.assertEqual(ResourceAssignment.objects.count(), 0)

        calendar = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )
        self.assertContains(calendar, "rTMS開始予定：2026/10/06")
        self.assertContains(calendar, "個室利用")
        self.assertContains(calendar, "全例調査対象")
        self.assertContains(calendar, "日程設定")
        self.assertNotContains(calendar, "course-adjustment-form")

    def test_weekly_summary_counts_courses_not_sessions(self):
        for session_date in (date(2026, 9, 21), date(2026, 9, 23)):
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                session_date=session_date,
                status="planned",
            )
        context = build_inpatient_calendar_range(
            start_date=date(2026, 9, 16),
            today=date(2026, 9, 16),
        )
        treatment_week = context["weeks"][1]
        self.assertEqual(treatment_week["rtms_course_count"], 1)
        self.assertEqual(context["weekly_metrics"][2]["weeks"][1]["value"], 1)

    def test_monthly_private_room_fallback_is_course_scoped_and_assignment_wins(self):
        self.course_one.private_room_planned = True
        self.course_one.save(update_fields=["private_room_planned", "updated_at"])
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 10, 5),
            planned_discharge_date=date(2026, 10, 10),
        )
        InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="scheduled",
            planned_admission_date=date(2026, 10, 5),
            planned_discharge_date=date(2026, 10, 10),
        )
        context = build_inpatient_calendar(year=2026, month=10)
        rows = {row["treatment_course_id"]: row for row in context["calendar_rows"]}
        first_room = rows[self.course_one.pk]["timelines"]["private_room"]
        second_room = rows[self.course_two.pk]["timelines"]["private_room"]
        self.assertEqual(len(first_room), 1)
        self.assertTrue(first_room[0]["planned_only"])
        self.assertEqual(second_room, [])
        day = next(day for day in context["days"] if day["date"] == date(2026, 10, 6))
        self.assertEqual(day["private_room_count"], 1)

        assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 7),
            planned_end_date=date(2026, 10, 8),
        )
        assigned_context = build_inpatient_calendar(year=2026, month=10)
        assigned_rows = {
            row["treatment_course_id"]: row
            for row in assigned_context["calendar_rows"]
        }
        assigned_room = assigned_rows[self.course_one.pk]["timelines"]["private_room"]
        self.assertEqual(len(assigned_room), 1)
        self.assertFalse(assigned_room[0]["planned_only"])
        self.assertEqual(assigned_room[0]["assignment"].pk, assignment.pk)

    def test_calendar_template_renders_timeline_context_for_selected_course(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            planned_discharge_date=date(2026, 10, 3),
        )
        TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 10, 2),
            status="planned",
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {
                "patient_id": self.patient.pk,
                "course_number": 2,
                "year": 2026,
                "month": 10,
            },
        )

        self.assertContains(response, "Patient ID 82001 / Course #2")
        self.assertContains(response, 'data-timeline-type="inpatient"')
        self.assertContains(response, 'data-timeline-type="rtms"')
        self.assertContains(response, 'data-course-number="2"')
        self.assertContains(response, 'data-start-date="2026-10-02"')
        self.assertContains(response, 'data-end-date="2026-10-02"')
        self.assertContains(response, "rTMS予定一覧")
        rendered = response.content.decode()
        self.assertNotIn("患者・Course別 Gantt", rendered)
        self.assertEqual(rendered.count('style="--timeline-weeks: 18;"'), 2)
        self.assertContains(response, "週別集計")
        self.assertContains(response, "入院者数")
        self.assertContains(response, "個室利用者数")
        self.assertContains(response, "rTMS治療者数")
        self.assertContains(response, "rTMS：予定1回 / 実施0回 / スキップ0回")

    def test_calendar_template_explains_unknown_end_resource_and_rtms_statuses(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            planned_admission_date=date(2026, 10, 1),
        )
        ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 3),
            planned_end_date=date(2026, 10, 5),
        )
        for session_date, status in (
            (date(2026, 10, 6), "planned"),
            (date(2026, 10, 7), "done"),
            (date(2026, 10, 8), "skipped"),
        ):
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                session_date=session_date,
                status=status,
            )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {
                "patient_id": self.patient.pk,
                "course_number": 1,
                "year": 2026,
                "month": 10,
            },
        )

        self.assertContains(response, 'data-end-kind="unknown"')
        self.assertContains(response, 'data-end-date=""')
        self.assertContains(response, "終了日不明")
        self.assertContains(response, self.pool.name)
        self.assertContains(response, 'data-start-date="2026-10-06"')
        self.assertContains(response, 'data-end-date="2026-10-08"')
        self.assertContains(response, "rTMS：予定1回 / 実施1回 / スキップ1回")

    def test_calendar_includes_active_waitlist_entries(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="waiting",
            priority=2,
        )
        response = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, "② 待機者リスト")
        self.assertContains(response, "Course #2")

    def test_calendar_month_navigation_uses_current_month_and_preserves_course_context(self):
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"patient_id": self.patient.pk, "course_number": 2},
        )

        self.assertEqual(response.status_code, 200)
        today = django_timezone.localdate()
        self.assertEqual(response.context["year"], today.year)
        self.assertEqual(response.context["month"], today.month)
        self.assertIn("patient_id={}&course_number=2".format(self.patient.pk), response.context["previous_month_url"])
        self.assertIn("patient_id={}&course_number=2".format(self.patient.pk), response.context["next_month_url"])

    def test_calendar_month_navigation_handles_requested_month_and_year_boundaries(self):
        october = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )
        self.assertEqual(october.context["year"], 2026)
        self.assertEqual(october.context["month"], 10)
        self.assertIn("year=2026&month=9", october.context["previous_month_url"])
        self.assertIn("year=2026&month=11", october.context["next_month_url"])

        january = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 1},
        )
        self.assertIn("year=2025&month=12", january.context["previous_month_url"])

        december = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 12},
        )
        self.assertIn("year=2027&month=1", december.context["next_month_url"])

    def test_calendar_today_link_returns_to_timezone_local_month(self):
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )

        today = django_timezone.localdate()
        self.assertIn(f"year={today.year}&month={today.month}", response.context["today_month_url"])

    def test_actual_discharge_overrides_planned_and_unknown_end_is_explicit(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one, status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            planned_discharge_date=date(2026, 10, 10),
            actual_discharge_at=datetime(2026, 10, 8, 10, tzinfo=timezone.utc),
        )
        context = build_inpatient_calendar(year=2026, month=10, treatment_course=self.course_one)
        day_eight = next(day for day in context["days"] if day["date"] == date(2026, 10, 8))
        day_nine = next(day for day in context["days"] if day["date"] == date(2026, 10, 9))
        self.assertEqual(day_eight["inpatient_count"], 1)
        self.assertEqual(day_nine["inpatient_count"], 0)
        plan.actual_discharge_at = None
        plan.planned_discharge_date = None
        plan.estimated_discharge_date = None
        plan.save(update_fields=["actual_discharge_at", "planned_discharge_date", "estimated_discharge_date", "updated_at"])
        unknown_context = build_inpatient_calendar(year=2026, month=10, treatment_course=self.course_one)
        self.assertTrue(any(day["unknown_end_count"] for day in unknown_context["days"]))
        unknown_timeline = unknown_context["calendar_rows"][0]["timelines"]["inpatient"][0]
        self.assertIsNone(unknown_timeline["end"])
        self.assertEqual(unknown_timeline["end_kind"], "unknown")

    def test_forecast_is_provisional_and_reports_unknown_discharge(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one, status="admitted",
            planned_admission_date=date(2026, 9, 1),
        )
        forecast = earliest_admission_forecast(date(2026, 10, 1), resource_pool=self.pool)
        self.assertIn("見込み", forecast["label"])
        self.assertEqual(forecast["uncertainty_count"], 1)

    def test_initial_visit_exposes_both_phase2_paths(self):
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )
        self.assertContains(response, reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]))
        self.assertContains(response, "rTMS待機者リストに登録")
        self.assertNotContains(response, "rTMS日程を相談")
        self.assertNotContains(response, "rTMS調整画面へ")
        self.assertNotContains(response, ">待機者登録</a>")

    def test_initial_visit_places_schedule_fields_in_requested_order(self):
        self.course_one.first_visit_date = date(2026, 9, 10)
        self.course_one.save(update_fields=["first_visit_date", "updated_at"])
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )
        content = response.content.decode()
        schedule_start = content.index("スケジュール・担当医")
        schedule = content[schedule_start:]
        self.assertIn('name="first_visit_date"', schedule)
        self.assertIn('value="2026-09-10"', schedule)
        field_order = (
            "初診日",
            "入院予定日",
            "初回治療日",
            "rTMS日程調整",
            "個室利用予定",
            "今後の担当医",
        )
        positions = [schedule.index(label) for label in field_order]
        self.assertEqual(positions, sorted(positions))

    def test_initial_visit_shows_registered_state_per_course(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
        )
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )
        self.assertContains(response, "待機者登録済み")
        self.assertNotContains(response, "rTMS待機者リストに登録")

        course_two_response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 2},
        )
        self.assertContains(course_two_response, "rTMS待機者リストに登録")

    def test_primary_calendar_navigation_links_are_available(self):
        dashboard = self.client.get(reverse("rtms_app:dashboard"))
        month = self.client.get(reverse("rtms_app:calendar_month"))
        rtms = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(dashboard, reverse("rtms_app:inpatient_calendar"))
        self.assertContains(month, reverse("rtms_app:dashboard"))
        self.assertContains(month, reverse("rtms_app:patient_list"))
        self.assertContains(month, reverse("rtms_app:inpatient_calendar"))
        self.assertContains(rtms, reverse("rtms_app:dashboard"))
        self.assertContains(rtms, reverse("rtms_app:patient_list"))
        self.assertContains(rtms, reverse("rtms_app:calendar_month"))

    def test_initial_visit_private_room_planned_is_course_scoped(self):
        physician_group = Group.objects.create(name="医師")
        self.user.groups.add(physician_group)
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )
        self.assertContains(response, "個室利用予定")
        self.client.post(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {
                "course_number": 1,
                "first_visit_date": "2026-09-16",
                "admission_date": "2026-10-01",
                "first_treatment_date": "2026-10-02",
                "attending_physician": str(self.user.pk),
                "private_room_planned": "on",
            },
        )
        self.course_one.refresh_from_db()
        self.course_two.refresh_from_db()
        self.assertTrue(self.course_one.private_room_planned)
        self.assertFalse(self.course_two.private_room_planned)
        calendar = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )
        self.assertContains(calendar, "終了日不明")
        self.assertContains(calendar, "個室利用予定")
        self.assertContains(calendar, "rTMS開始予定：2026/10/02")

    def test_phase2_entry_paths_render_for_selected_course(self):
        calendar_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"patient_id": self.patient.pk, "course_number": self.course_one.course_number},
        )
        waitlist_response = self.client.get(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]),
            {"course_number": self.course_one.course_number},
        )
        self.assertEqual(calendar_response.status_code, 200)
        self.assertEqual(waitlist_response.status_code, 200)
        self.assertContains(calendar_response, "選択中: Phase Two")
        self.assertContains(waitlist_response, "rTMS待機リスト")

    def test_calendar_reorganizes_global_view_with_waitlist_and_course_filter(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            planned_discharge_date=date(2026, 10, 10),
        )
        InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="provisional",
            planned_admission_date=date(2026, 10, 12),
            planned_discharge_date=date(2026, 10, 20),
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            priority=2,
            preferred_start_note="10月中旬希望",
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="requested",
            priority=1,
            preferred_start_note="11月希望",
        )

        global_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )
        self.assertEqual(global_response.status_code, 200)
        self.assertContains(global_response, "rTMS調整カレンダー")
        self.assertContains(global_response, "① rTMS予定一覧")
        self.assertNotContains(global_response, "② 期間集計")
        self.assertNotContains(global_response, "表示対象:")
        self.assertContains(global_response, '>区分</div>')
        self.assertContains(global_response, 'timeline-lane-label">入院</div>')
        self.assertContains(global_response, 'timeline-lane-label">個室</div>')
        self.assertContains(global_response, 'timeline-lane-label">rTMS</div>')
        self.assertContains(global_response, "timeline-header-row")
        self.assertContains(global_response, "timeline-course-group")
        self.assertContains(global_response, "② 待機者リスト")
        self.assertNotContains(global_response, "④ 日別詳細")
        self.assertNotContains(global_response, "日別詳細を表示")
        self.assertContains(global_response, "Course #1")
        self.assertContains(global_response, "Course #2")
        self.assertContains(global_response, "＋ rTMS待機者登録")
        self.assertContains(global_response, "日程設定")
        self.assertNotContains(global_response, ">待機者登録</a>")
        self.assertContains(
            global_response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=1",
        )
        self.assertContains(
            global_response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=2",
        )
        self.assertContains(global_response, "data-course-id=\"{}\"".format(self.course_one.pk))
        self.assertContains(global_response, "data-course-id=\"{}\"".format(self.course_two.pk))

        filtered_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {
                "patient_id": self.patient.pk,
                "course_number": 1,
                "year": 2026,
                "month": 10,
            },
        )
        self.assertContains(filtered_response, "選択中: Phase Two")
        self.assertContains(filtered_response, "Course #1")
        self.assertContains(filtered_response, "治療開始希望時期・備考")
        self.assertContains(filtered_response, "10月中旬希望")
        self.assertNotContains(filtered_response, "2026/11/01")

    def test_weekly_private_room_planned_fallback_and_assignment_priority(self):
        self.course_one.private_room_planned = True
        self.course_one.save(update_fields=["private_room_planned", "updated_at"])
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 18),
            planned_discharge_date=date(2026, 10, 2),
        )
        planned_context = build_inpatient_calendar_range(
            start_date=date(2026, 9, 16),
            today=date(2026, 9, 16),
            treatment_course=self.course_one,
        )
        planned_item = planned_context["weekly_calendar_rows"][0]["timelines"]["private_room"][0]
        self.assertTrue(planned_item["planned_only"])
        self.assertIsNone(planned_item["resource_pool"])
        self.assertEqual(planned_context["weekly_summary"]["peak_private_rooms"], 1)

        assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 9, 28),
            planned_end_date=date(2026, 10, 2),
        )
        assigned_context = build_inpatient_calendar_range(
            start_date=date(2026, 9, 16),
            today=date(2026, 9, 16),
            treatment_course=self.course_one,
        )
        assigned_item = assigned_context["weekly_calendar_rows"][0]["timelines"]["private_room"][0]
        self.assertFalse(assigned_item["planned_only"])
        self.assertEqual(assigned_item["assignment"].pk, assignment.pk)
        self.assertEqual(assigned_item["start"], assignment.planned_start_date)

    def test_waitlist_post_redirects_with_patient_and_course_and_keeps_isolation(self):
        response = self.client.post(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk])
            + "?course_number=1",
            {
                "preferred_start_note": "2026/10/15〜10/20希望、入院14日程度",
                "priority": "2",
                "comment": "希望日程あり",
                "course_number": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk])
            + "?course_number=1",
        )
        entry = self.course_one.rtms_waitlist_entries.get()
        self.assertEqual(entry.status, "waiting")
        self.assertEqual(entry.preferred_start_note, "2026/10/15〜10/20希望、入院14日程度")
        self.assertEqual(entry.comment, "希望日程あり")
        self.assertEqual(ResourceAssignment.objects.count(), 0)

        course_two_response = self.client.get(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk])
            + "?course_number=2",
        )
        self.assertEqual(course_two_response.status_code, 200)
        self.assertNotContains(course_two_response, "希望日程あり")

    def test_waitlist_registration_ui_omits_priority(self):
        response = self.client.get(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]),
            {"course_number": 1},
        )
        self.assertNotContains(response, "priority")
        self.assertNotContains(response, "優先度")
        self.assertContains(response, "治療開始希望時期・備考")
        self.assertNotContains(response, "希望開始時期（早い方）")
        self.assertNotContains(response, "希望開始時期（遅い方）")
        self.assertNotContains(response, "希望入院日数")

    def test_waitlist_registration_stores_editable_planned_treatment_count(self):
        response = self.client.post(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]) + "?course_number=1",
            {"planned_treatment_sessions": "24", "course_number": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.course_one.refresh_from_db()
        self.assertEqual(self.course_one.planned_treatment_sessions, 24)
        self.assertNotContains(
            self.client.get(
                reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]),
                {"course_number": 1},
            ),
            "希望治療期間",
        )

    def test_planned_treatment_count_defaults_to_30_per_course(self):
        self.assertEqual(self.course_one.planned_treatment_sessions, 30)
        self.assertEqual(self.course_two.planned_treatment_sessions, 30)

    def test_waitlist_schedule_expansion_is_compact_and_not_always_open(self):
        entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            preferred_start_note="10月中旬希望",
        )
        response = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, "日程設定")
        self.assertNotContains(response, 'details class="waitlist-adjustment" open')
        self.assertContains(response, "治療開始希望時期・備考")
        self.assertNotContains(response, "希望開始日")
        self.assertNotContains(response, "希望終了日")
        self.assertNotContains(response, "希望入院日数")
        self.assertContains(response, "waitlist-adjustment-grid")
        self.assertContains(response, "waitlist-adjustment-flags")
        self.assertNotContains(response, ">待機者登録</a>")
        self.assertContains(response, f'name="waitlist_entry_id" value="{entry.pk}"')

    def test_waitlist_schedule_saves_preferences_course_fields_and_no_sessions(self):
        entry_one = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            preferred_start_note="10月希望",
        )
        entry_two = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="waiting",
            preferred_start_note="11月希望",
        )
        response = self.client.post(
            reverse("rtms_app:inpatient_calendar"),
            {
                "action": "save_calendar_course",
                "treatment_course_id": self.course_one.pk,
                "waitlist_entry_id": entry_one.pk,
                "preferred_start_note": "2026/10/15〜10/20希望",
                "admission_date": "2026-10-05",
                "first_treatment_date": "2026-10-08",
                "private_room_planned": "on",
                "is_all_case_survey": "on",
                "planned_treatment_sessions": "28",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.course_one.refresh_from_db()
        self.course_two.refresh_from_db()
        entry_one.refresh_from_db()
        entry_two.refresh_from_db()
        self.assertEqual(entry_one.preferred_start_note, "2026/10/15〜10/20希望")
        self.assertEqual(self.course_one.admission_date, date(2026, 10, 5))
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 8))
        self.assertTrue(self.course_one.private_room_planned)
        self.assertTrue(self.course_one.is_all_case_survey)
        self.assertEqual(self.course_one.planned_treatment_sessions, 28)
        self.assertIsNone(self.course_two.admission_date)
        self.assertEqual(entry_two.preferred_start_note, "11月希望")
        self.assertEqual(TreatmentSession.objects.count(), 0)

    def test_course_without_treatment_start_has_no_virtual_assessment_events(self):
        calendar_weeks, assessment_events = generate_calendar_weeks(
            self.patient, treatment_course=self.course_one,
        )
        self.assertTrue(calendar_weeks)
        self.assertEqual(assessment_events, [])
        self.assertEqual(Assessment.objects.count(), 0)
        self.assertEqual(AssessmentRecord.objects.count(), 0)

    def test_legacy_management_waitlist_redirects_to_calendar(self):
        response = self.client.get(reverse("rtms_app:inpatient_waitlist"))
        self.assertRedirects(response, reverse("rtms_app:inpatient_calendar"))

    def test_management_waitlist_lists_both_courses_and_excludes_non_waiting_statuses(self):
        registered_at = timezone_now = datetime.now(timezone.utc) - timedelta(days=6)
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            priority=2,
            registered_at=registered_at,
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="requested",
            priority=1,
            registered_at=registered_at + timedelta(days=1),
        )
        scheduled_course = TreatmentCourse.objects.create(
            patient=self.patient, course_number=3,
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=scheduled_course,
            status="scheduled",
            priority=9,
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="cancelled",
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="withdrawn",
        )

        response = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, "Course #1")
        self.assertContains(response, "Course #2")
        self.assertContains(response, "② 待機者リスト")
        self.assertEqual(ResourceAssignment.objects.count(), 0)
        self.assertEqual(InpatientPlan.objects.count(), 0)

    def test_management_waitlist_empty_state_and_status_filter(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
        )

        response = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, "現在、rTMS待機者はいません。")

    def test_calendar_waitlist_rows_preserve_each_course(self):
        self.course_one.admission_date = date(2026, 10, 1)
        self.course_two.admission_date = date(2026, 10, 8)
        self.course_one.save(update_fields=["admission_date", "updated_at"])
        self.course_two.save(update_fields=["admission_date", "updated_at"])
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="waiting",
        )

        response = self.client.get(reverse("rtms_app:inpatient_calendar"))

        self.assertContains(
            response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=1",
        )
        self.assertContains(
            response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=2",
        )

    def test_waitlist_registration_patient_selection_searches_id_and_name(self):
        response = self.client.get(
            reverse("rtms_app:rtms_waitlist_patient_select"),
            {"card": self.patient.card_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Course #1")
        self.assertContains(response, "Course #2")

        name_response = self.client.get(
            reverse("rtms_app:rtms_waitlist_patient_select"),
            {"q": "Phase Two"},
        )
        self.assertContains(name_response, "Course #1")
        self.assertContains(name_response, "Course #2")

    def test_waitlist_registration_patient_selection_preserves_course(self):
        response = self.client.get(reverse("rtms_app:rtms_waitlist_patient_select"))
        self.assertContains(
            response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=1",
        )
        self.assertContains(
            response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=2",
        )

    def test_calendar_navigation_preserves_course_from_waitlist_destination(self):
        calendar_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {
                "patient_id": self.patient.pk,
                "course_number": self.course_two.course_number,
                "year": 2026,
                "month": 10,
            },
        )

        self.assertEqual(calendar_response.context["treatment_course"], self.course_two)
        for link_name in ("previous_month_url", "next_month_url", "today_month_url"):
            self.assertIn(f"patient_id={self.patient.pk}", calendar_response.context[link_name])
            self.assertIn("course_number=2", calendar_response.context[link_name])
