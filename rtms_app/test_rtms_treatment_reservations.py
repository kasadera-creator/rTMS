import json
from datetime import date, timedelta

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from rtms_app.models import (
    InpatientPlan,
    Patient,
    ResourceAssignment,
    ResourcePool,
    RtmSAdmissionCapacity,
    RtmSTreatmentCapacity,
    RtmSTreatmentReservation,
    RtmSWaitlistEntry,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services.rtms_schedule import (
    generate_course_treatment_dates,
    generate_treatment_dates,
    get_japanese_holidays,
)
from rtms_app.services.rtms_treatment_reservations import (
    InvalidTreatmentReservationError,
    TreatmentCapacityConfigurationError,
    TreatmentReservationCapacityConflict,
    confirm_rtms_treatment_reservation,
    create_or_replace_rtms_treatment_reservation,
    estimate_available_new_courses,
    estimate_available_new_courses_by_start_dates,
    preview_rtms_treatment_reservation,
    sync_rtms_treatment_reservation_from_sessions,
    _total_occupancy_for_dates,
    treatment_capacity_snapshot,
    treatment_capacity_snapshots,
)
from rtms_app.services.inpatient_planning import update_calendar_course_adjustment
from rtms_app.services.inpatient_planning import confirm_inpatient_schedule
from rtms_app.services.schedule import (
    reschedule_treatment_start_date,
    shift_future_sessions,
)


class RtmsTreatmentReservationTests(TestCase):
    def setUp(self):
        self.private_capacity = RtmSTreatmentCapacity.objects.get(
            capacity_type="rTMS_private",
            valid_from=date(2026, 1, 1),
        )
        self.general_capacity = RtmSTreatmentCapacity.objects.get(
            capacity_type="general_ward",
            valid_from=date(2026, 1, 1),
        )
        self.card_sequence = 85000

    def make_course(self):
        self.card_sequence += 1
        patient = Patient.objects.create(
            card_id=str(self.card_sequence),
            name=f"Reservation patient {self.card_sequence}",
            birth_date=date(1980, 1, 1),
        )
        return TreatmentCourse.objects.create(patient=patient, course_number=1)

    def book(self, course, start_date=date(2026, 10, 5), **kwargs):
        return create_or_replace_rtms_treatment_reservation(
            treatment_course=course,
            admission_date=start_date,
            first_treatment_date=start_date,
            **kwargs,
        )

    def test_initial_capacity_and_admin_edit(self):
        snapshot = treatment_capacity_snapshot(date(2026, 10, 5))
        self.assertEqual(snapshot["rTMS_private"]["capacity"], 3)
        self.assertEqual(snapshot["general_ward"]["capacity"], 1)
        self.assertEqual(snapshot["total_capacity"], 4)

        request = RequestFactory().get("/admin/")
        model_admin = admin.site._registry[RtmSTreatmentCapacity]
        form_class = model_admin.get_form(request, obj=self.private_capacity)
        form = form_class(
            data={
                "capacity_type": "rTMS_private",
                "capacity": "2",
                "valid_from": "2026-01-01",
                "valid_to": "",
                "is_active": "on",
            },
            instance=self.private_capacity,
        )
        self.assertIn("capacity", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            treatment_capacity_snapshot(date(2026, 10, 5))["rTMS_private"]["capacity"],
            2,
        )

    def test_zero_capacity_and_overlapping_period_validation(self):
        self.private_capacity.capacity = 0
        self.private_capacity.full_clean()
        self.private_capacity.save()
        self.assertEqual(
            treatment_capacity_snapshot(date(2026, 10, 5))["rTMS_private"]["remaining"],
            0,
        )

        overlapping = RtmSTreatmentCapacity(
            capacity_type="rTMS_private",
            capacity=1,
            valid_from=date(2026, 10, 1),
        )
        with self.assertRaises(ValidationError):
            overlapping.full_clean()

    def test_future_capacity_periods_are_supported(self):
        self.private_capacity.valid_to = date(2026, 12, 31)
        self.private_capacity.save(update_fields=["valid_to", "updated_at"])
        self.general_capacity.valid_to = date(2026, 12, 31)
        self.general_capacity.save(update_fields=["valid_to", "updated_at"])
        RtmSTreatmentCapacity.objects.create(
            capacity_type="rTMS_private", capacity=2, valid_from=date(2027, 1, 1),
        )
        RtmSTreatmentCapacity.objects.create(
            capacity_type="general_ward", capacity=2, valid_from=date(2027, 1, 1),
        )

        snapshot = treatment_capacity_snapshot(date(2027, 1, 4))
        self.assertEqual(snapshot["rTMS_private"]["capacity"], 2)
        self.assertEqual(snapshot["general_ward"]["capacity"], 2)

    def test_estimate_uses_current_capacity_and_reflects_admin_changes(self):
        start_date = date(2026, 10, 5)
        self.assertEqual(estimate_available_new_courses(start_date), 4)

        self.private_capacity.capacity = 8
        self.private_capacity.save(update_fields=["capacity", "updated_at"])
        self.general_capacity.capacity = 4
        self.general_capacity.save(update_fields=["capacity", "updated_at"])

        self.assertEqual(estimate_available_new_courses(start_date), 12)

    def test_estimator_counts_treatment_sessions_without_reservations(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        dates = generate_treatment_dates(start_date, total=30)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ])

        self.assertEqual(estimate_available_new_courses(start_date), 3)
        self.assertFalse(RtmSTreatmentReservation.objects.filter(treatment_course=course).exists())

    def test_treatment_add_syncs_thirty_sessions_and_updates_estimator(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        course.admission_date = start_date
        course.first_treatment_date = start_date
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        dates = generate_treatment_dates(
            start_date,
            total=30,
            holidays=get_japanese_holidays(start_date, start_date + timedelta(days=365)),
        )
        user = get_user_model().objects.create_user(username="treatment-add-reservation")
        self.client.force_login(user)
        endpoint = reverse("rtms_app:treatment_add", args=[course.patient_id])

        self.assertEqual(estimate_available_new_courses(start_date), 3)
        for treatment_date in dates[:29]:
            response = self.client.post(endpoint, {
                "course_number": course.course_number,
                "treatment_date": treatment_date.isoformat(),
                "treatment_time": "09:00",
                "mt_percent": "120",
                "intensity_percent": "120",
                "frequency_hz": "18.0",
                "train_seconds": "2.0",
                "intertrain_seconds": "20.0",
                "train_count": "55",
                "total_pulses": "1980",
                "side_effect_rows_json": "[]",
            })
            self.assertIn(response.status_code, (200, 302), response.content)
        self.assertEqual(
            TreatmentSession.objects.filter(treatment_course=course).count(),
            29,
        )
        self.assertFalse(RtmSTreatmentReservation.objects.filter(treatment_course=course).exists())
        self.assertEqual(estimate_available_new_courses(start_date), 3)

        response = self.client.post(endpoint, {
            "course_number": course.course_number,
            "treatment_date": dates[29].isoformat(),
            "treatment_time": "09:00",
            "mt_percent": "120",
            "intensity_percent": "120",
            "frequency_hz": "18.0",
            "train_seconds": "2.0",
            "intertrain_seconds": "20.0",
            "train_count": "55",
            "total_pulses": "1980",
            "side_effect_rows_json": "[]",
        })
        self.assertIn(response.status_code, (200, 302), response.content)

        sessions = list(
            TreatmentSession.objects.filter(treatment_course=course)
            .order_by("session_date", "pk")
        )
        reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .order_by("sequence_number")
        )
        self.assertEqual(len(sessions), 30)
        self.assertEqual(
            TreatmentSession.objects.filter(
                treatment_course=course,
                status="planned",
            ).count(),
            30,
        )
        self.assertEqual(len(reservations), 30)
        self.assertTrue(all(
            reservation.status in {"provisional", "confirmed"}
            for reservation in reservations
        ))
        self.assertEqual(
            [session.session_date for session in sessions],
            [reservation.treatment_date for reservation in reservations],
        )
        self.assertEqual(estimate_available_new_courses(start_date), 3)
        reservation_ids = {
            reservation.sequence_number: reservation.pk
            for reservation in reservations
        }

        response = self.client.post(endpoint, {
            "course_number": course.course_number,
            "treatment_date": dates[29].isoformat(),
            "treatment_time": "09:00",
            "mt_percent": "120",
            "intensity_percent": "120",
            "frequency_hz": "18.0",
            "train_seconds": "2.0",
            "intertrain_seconds": "20.0",
            "train_count": "55",
            "total_pulses": "1980",
            "side_effect_rows_json": "[]",
        })
        self.assertIn(response.status_code, (200, 302), response.content)
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(treatment_course=course).count(),
            30,
        )
        self.assertEqual(
            dict(RtmSTreatmentReservation.objects.filter(treatment_course=course)
                 .values_list("sequence_number", "pk")),
            reservation_ids,
        )
        self.assertEqual(estimate_available_new_courses(start_date), 3)

        RtmSTreatmentReservation.objects.filter(treatment_course=course).update(
            status="cancelled",
        )
        self.assertEqual(estimate_available_new_courses(start_date), 3)

    def test_estimate_uses_future_capacity_periods_for_all_thirty_dates(self):
        self.private_capacity.valid_to = date(2026, 12, 31)
        self.private_capacity.save(update_fields=["valid_to", "updated_at"])
        self.general_capacity.valid_to = date(2026, 12, 31)
        self.general_capacity.save(update_fields=["valid_to", "updated_at"])
        RtmSTreatmentCapacity.objects.create(
            capacity_type="rTMS_private", capacity=8, valid_from=date(2027, 1, 1),
        )
        RtmSTreatmentCapacity.objects.create(
            capacity_type="general_ward", capacity=4, valid_from=date(2027, 1, 1),
        )

        self.assertEqual(estimate_available_new_courses(date(2027, 1, 4)), 12)

    def test_estimate_respects_active_reservation_statuses(self):
        start_date = date(2026, 10, 5)
        holidays = get_japanese_holidays(start_date, start_date + timedelta(days=365))
        treatment_date = generate_treatment_dates(start_date, total=30, holidays=holidays)[0]
        for status in ("provisional", "confirmed", "cancelled"):
            RtmSTreatmentReservation.objects.create(
                treatment_course=self.make_course(),
                treatment_date=treatment_date,
                sequence_number=1,
                status=status,
                capacity_type="rTMS_private",
            )

        self.assertEqual(estimate_available_new_courses(start_date), 2)

    def test_estimate_requires_capacity_on_all_thirty_dates_and_counts_each_candidate(self):
        start_date = date(2026, 10, 5)
        expected_slots = 4
        for _ in range(4):
            self.book(self.make_course(), start_date=start_date)
            expected_slots -= 1
            self.assertEqual(estimate_available_new_courses(start_date), expected_slots)

    def test_estimate_rejects_a_candidate_when_one_treatment_date_is_full(self):
        start_date = date(2026, 10, 5)
        holidays = get_japanese_holidays(start_date, start_date + timedelta(days=365))
        blocked_date = generate_treatment_dates(start_date, total=30, holidays=holidays)[15]
        reservations = []
        for index, capacity_type in enumerate(
            ("rTMS_private", "rTMS_private", "rTMS_private", "general_ward"),
        ):
            reservations.append(RtmSTreatmentReservation(
                treatment_course=self.make_course(),
                treatment_date=blocked_date,
                sequence_number=1,
                status="confirmed",
                capacity_type=capacity_type,
            ))
        RtmSTreatmentReservation.objects.bulk_create(reservations)

        self.assertEqual(estimate_available_new_courses(start_date), 0)

    def test_estimate_moves_monday_holiday_start_to_first_open_day(self):
        week_start = date(2026, 10, 12)
        first_open_date = date(2026, 10, 13)
        holidays = get_japanese_holidays(week_start, week_start + timedelta(days=365))
        self.assertIn(week_start, holidays)
        self.assertEqual(
            generate_treatment_dates(week_start, total=30, holidays=holidays)[0],
            first_open_date,
        )
        reservations = [
            RtmSTreatmentReservation(
                treatment_course=self.make_course(),
                treatment_date=first_open_date,
                sequence_number=1,
                status="confirmed",
                capacity_type=capacity_type,
            )
            for capacity_type in (
                "rTMS_private", "rTMS_private", "rTMS_private", "general_ward",
            )
        ]
        RtmSTreatmentReservation.objects.bulk_create(reservations)

        self.assertEqual(estimate_available_new_courses(week_start), 0)

    def test_estimate_respects_year_end_closure_across_the_thirty_dates(self):
        week_start = date(2026, 12, 28)
        holidays = get_japanese_holidays(week_start, week_start + timedelta(days=365))
        treatment_dates = generate_treatment_dates(
            week_start,
            total=30,
            holidays=holidays,
        )
        self.assertEqual(treatment_dates[1], date(2027, 1, 4))
        reservations = [
            RtmSTreatmentReservation(
                treatment_course=self.make_course(),
                treatment_date=treatment_dates[1],
                sequence_number=2,
                status="confirmed",
                capacity_type=capacity_type,
            )
            for capacity_type in (
                "rTMS_private", "rTMS_private", "rTMS_private", "general_ward",
            )
        ]
        RtmSTreatmentReservation.objects.bulk_create(reservations)

        self.assertEqual(estimate_available_new_courses(week_start), 0)

    def test_estimate_includes_reservations_after_the_twelve_week_display(self):
        range_start = date(2026, 9, 7)
        final_week_start = range_start + timedelta(weeks=11)
        display_end = final_week_start + timedelta(days=6)
        holidays = get_japanese_holidays(
            final_week_start,
            final_week_start + timedelta(days=365),
        )
        last_treatment_date = generate_treatment_dates(
            final_week_start,
            total=30,
            holidays=holidays,
        )[-1]
        self.assertGreater(last_treatment_date, display_end)
        reservations = [
            RtmSTreatmentReservation(
                treatment_course=self.make_course(),
                treatment_date=last_treatment_date,
                sequence_number=30,
                status="confirmed",
                capacity_type=capacity_type,
            )
            for capacity_type in (
                "rTMS_private", "rTMS_private", "rTMS_private", "general_ward",
            )
        ]
        RtmSTreatmentReservation.objects.bulk_create(reservations)

        result = estimate_available_new_courses_by_start_dates([final_week_start])

        self.assertEqual(result[final_week_start], 0)

    def test_weekly_estimates_are_independent_bulk_and_read_only(self):
        self.private_capacity.capacity = 1
        self.private_capacity.save(update_fields=["capacity", "updated_at"])
        self.general_capacity.capacity = 0
        self.general_capacity.save(update_fields=["capacity", "updated_at"])
        week_starts = [date(2026, 10, 5), date(2026, 10, 12)]
        before = (
            RtmSTreatmentReservation.objects.count(),
            TreatmentCourse.objects.count(),
            TreatmentSession.objects.count(),
        )

        with self.assertNumQueries(4):
            estimates = estimate_available_new_courses_by_start_dates(week_starts)

        after = (
            RtmSTreatmentReservation.objects.count(),
            TreatmentCourse.objects.count(),
            TreatmentSession.objects.count(),
        )
        self.assertEqual(estimates, {week_starts[0]: 1, week_starts[1]: 1})
        self.assertEqual(after, before)

    def test_read_only_snapshot_marks_missing_capacity_as_unknown(self):
        RtmSTreatmentCapacity.objects.all().delete()
        treatment_date = date(2025, 12, 29)

        snapshot = treatment_capacity_snapshots(
            [treatment_date],
            allow_unconfigured=True,
        )[0]

        self.assertIsNone(snapshot["rTMS_private"]["capacity"])
        self.assertIsNone(snapshot["rTMS_private"]["remaining"])
        self.assertIsNone(snapshot["general_ward"]["capacity"])
        self.assertIsNone(snapshot["total_capacity"])
        self.assertIsNone(snapshot["total_remaining"])
        self.assertEqual(snapshot["total_occupied"], 0)

    def test_estimator_marks_unconfigured_course_horizon_as_unavailable(self):
        RtmSTreatmentCapacity.objects.all().delete()
        start_date = date(2025, 12, 22)

        estimates = estimate_available_new_courses_by_start_dates([start_date])

        self.assertIsNone(estimates[start_date])

    def test_reservation_write_still_rejects_missing_capacity(self):
        RtmSTreatmentCapacity.objects.all().delete()
        course = self.make_course()
        before = RtmSTreatmentReservation.objects.count()

        with self.assertRaises(TreatmentCapacityConfigurationError):
            self.book(course, start_date=date(2025, 12, 22))

        self.assertEqual(RtmSTreatmentReservation.objects.count(), before)

    def test_course_only_plan_counts_as_occupancy_for_all_thirty_dates(self):
        start_date = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        dates = generate_treatment_dates(
            start_date,
            total=30,
            holidays=get_japanese_holidays(start_date, start_date + timedelta(days=365)),
        )

        occupancy = _total_occupancy_for_dates(dates)

        self.assertEqual(len(dates), 30)
        self.assertEqual([occupancy[treatment_date] for treatment_date in dates], [1] * 30)
        self.assertEqual(estimate_available_new_courses(start_date), 3)

    def test_course_plan_and_session_on_same_date_count_once(self):
        start_date = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        dates = generate_treatment_dates(start_date, total=30)
        TreatmentSession.objects.create(
            patient=course.patient,
            treatment_course=course,
            course_number=course.course_number,
            session_date=dates[10],
            status="planned",
        )

        occupancy = _total_occupancy_for_dates([dates[10]])

        self.assertEqual(occupancy[dates[10]], 1)

    def test_course_plan_and_active_reservation_on_same_date_count_once(self):
        start_date = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        RtmSTreatmentReservation.objects.create(
            treatment_course=course,
            treatment_date=start_date,
            sequence_number=1,
            status="confirmed",
            capacity_type="general_ward",
        )

        occupancy = _total_occupancy_for_dates([start_date])

        self.assertEqual(occupancy[start_date], 1)

    def test_skipped_session_does_not_count_as_occupancy_or_course_plan(self):
        start_date = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        dates = generate_course_treatment_dates(start_date)
        TreatmentSession.objects.create(
            patient=course.patient,
            treatment_course=course,
            course_number=course.course_number,
            session_date=dates[0],
            status="skipped",
        )

        occupancy = _total_occupancy_for_dates(dates)

        self.assertEqual(occupancy[dates[0]], 0)
        self.assertTrue(all(occupancy[treatment_date] == 1 for treatment_date in dates[1:]))

    def test_partially_materialized_course_occupies_derived_dates_without_duplicates(self):
        start_date = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        dates = generate_course_treatment_dates(start_date)
        TreatmentSession.objects.create(
            patient=course.patient,
            treatment_course=course,
            course_number=course.course_number,
            session_date=dates[0],
            status="planned",
        )
        RtmSTreatmentReservation.objects.create(
            treatment_course=course,
            treatment_date=dates[0],
            sequence_number=1,
            status="confirmed",
            capacity_type="general_ward",
        )

        occupancy = _total_occupancy_for_dates(dates)

        self.assertEqual([occupancy[treatment_date] for treatment_date in dates], [1] * 30)

    def test_six_materialized_sessions_leave_course_derived_occupancy(self):
        start_date = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        dates = generate_course_treatment_dates(start_date)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates[:6]
        ])

        occupancy = _total_occupancy_for_dates(dates)

        self.assertEqual([occupancy[treatment_date] for treatment_date in dates], [1] * 30)

    def test_thirty_materialized_sessions_are_not_double_counted_with_course_plan(self):
        start_date = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        dates = generate_course_treatment_dates(start_date)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ])

        occupancy = _total_occupancy_for_dates(dates)

        self.assertEqual([occupancy[treatment_date] for treatment_date in dates], [1] * 30)

    def test_course_derived_occupancy_is_independent_between_candidate_weeks(self):
        first_week = date(2026, 10, 5)
        later_week = date(2026, 11, 23)
        course = self.make_course()
        course.first_treatment_date = first_week
        course.save(update_fields=["first_treatment_date", "updated_at"])

        estimates = estimate_available_new_courses_by_start_dates([first_week, later_week])

        self.assertEqual(estimates, {first_week: 3, later_week: 4})

    def test_course_plan_starting_mid_window_reduces_capacity(self):
        candidate_start = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = date(2026, 10, 20)
        course.save(update_fields=["first_treatment_date", "updated_at"])
        candidate_dates = generate_treatment_dates(candidate_start, total=30)

        occupancy = _total_occupancy_for_dates(candidate_dates)

        self.assertEqual(occupancy[candidate_dates[0]], 0)
        self.assertGreater(max(occupancy.values()), 0)
        self.assertEqual(estimate_available_new_courses(candidate_start), 3)

    def test_two_course_plans_reduce_june_fifteenth_capacity_to_two(self):
        for _ in range(2):
            course = self.make_course()
            course.first_treatment_date = date(2026, 6, 22)
            course.save(update_fields=["first_treatment_date", "updated_at"])

        self.assertEqual(estimate_available_new_courses(date(2026, 6, 15)), 2)

    def test_later_three_course_plans_leave_one_new_start_slot(self):
        candidate_start = date(2026, 8, 3)
        for _ in range(3):
            course = self.make_course()
            course.first_treatment_date = candidate_start
            course.save(update_fields=["first_treatment_date", "updated_at"])

        self.assertEqual(estimate_available_new_courses(candidate_start), 1)

    def test_june_twenty_second_candidate_leaves_one_when_later_occupancy_is_three(self):
        first_course = self.make_course()
        first_course.first_treatment_date = date(2026, 6, 15)
        first_course.save(update_fields=["first_treatment_date", "updated_at"])
        second_course = self.make_course()
        second_course.first_treatment_date = date(2026, 6, 22)
        second_course.save(update_fields=["first_treatment_date", "updated_at"])
        later_date = date(2026, 8, 3)
        for _ in range(2):
            session_course = self.make_course()
            TreatmentSession.objects.create(
                patient=session_course.patient,
                treatment_course=session_course,
                course_number=session_course.course_number,
                session_date=later_date,
                status="planned",
            )

        self.assertEqual(estimate_available_new_courses(date(2026, 6, 22)), 1)

    def test_session_and_reservation_for_course_plan_count_once(self):
        start_date = date(2026, 10, 5)
        course = self.make_course()
        course.first_treatment_date = start_date
        course.save(update_fields=["first_treatment_date", "updated_at"])
        TreatmentSession.objects.create(
            patient=course.patient,
            treatment_course=course,
            course_number=course.course_number,
            session_date=start_date,
            status="planned",
        )
        RtmSTreatmentReservation.objects.create(
            treatment_course=course,
            treatment_date=start_date,
            sequence_number=1,
            status="provisional",
            capacity_type="rTMS_private",
        )

        occupancy = _total_occupancy_for_dates([start_date])

        self.assertEqual(occupancy[start_date], 1)

    def test_weekly_estimator_uses_total_reservations_and_session_fallback_without_double_count(self):
        week_start = date(2026, 10, 5)
        first_treatment_date = week_start
        second_treatment_date = date(2026, 10, 6)
        self.assertEqual(estimate_available_new_courses(week_start), 4)

        reserved_course = self.make_course()
        RtmSTreatmentReservation.objects.create(
            treatment_course=reserved_course,
            treatment_date=first_treatment_date,
            sequence_number=1,
            status="confirmed",
            capacity_type="general_ward",
        )
        self.assertEqual(estimate_available_new_courses(week_start), 3)

        planned_course = self.make_course()
        TreatmentSession.objects.create(
            patient=planned_course.patient,
            treatment_course=planned_course,
            course_number=planned_course.course_number,
            session_date=first_treatment_date,
            status="planned",
        )
        self.assertEqual(estimate_available_new_courses(week_start), 2)

        paired_course = self.make_course()
        TreatmentSession.objects.create(
            patient=paired_course.patient,
            treatment_course=paired_course,
            course_number=paired_course.course_number,
            session_date=first_treatment_date,
            status="planned",
        )
        RtmSTreatmentReservation.objects.create(
            treatment_course=paired_course,
            treatment_date=second_treatment_date,
            sequence_number=1,
            status="provisional",
            capacity_type="rTMS_private",
        )
        self.assertEqual(estimate_available_new_courses(week_start), 2)

        skipped_course = self.make_course()
        TreatmentSession.objects.create(
            patient=skipped_course.patient,
            treatment_course=skipped_course,
            course_number=skipped_course.course_number,
            session_date=first_treatment_date,
            status="skipped",
        )
        self.assertEqual(estimate_available_new_courses(week_start), 2)

        done_course = self.make_course()
        TreatmentSession.objects.create(
            patient=done_course.patient,
            treatment_course=done_course,
            course_number=done_course.course_number,
            session_date=first_treatment_date,
            status="done",
        )
        before = (
            RtmSTreatmentReservation.objects.count(),
            TreatmentSession.objects.count(),
        )

        self.assertEqual(estimate_available_new_courses(week_start), 1)
        self.assertEqual(
            (RtmSTreatmentReservation.objects.count(), TreatmentSession.objects.count()),
            before,
        )

    def test_weekly_estimator_counts_and_deduplicates_legacy_sessions(self):
        week_start = date(2026, 10, 5)
        reserved_course = self.make_course()
        self.book(reserved_course, start_date=week_start)
        self.assertEqual(estimate_available_new_courses(week_start), 3)

        TreatmentSession.objects.create(
            patient=reserved_course.patient,
            treatment_course=None,
            course_number=reserved_course.course_number,
            session_date=week_start,
            status="planned",
        )
        self.assertEqual(estimate_available_new_courses(week_start), 3)

        legacy_patient = Patient.objects.create(
            card_id="LEGACY-CAPACITY",
            name="Legacy capacity patient",
            birth_date=date(1980, 1, 1),
        )
        TreatmentSession.objects.create(
            patient=legacy_patient,
            treatment_course=None,
            course_number=1,
            session_date=week_start,
            status="done",
        )
        self.assertEqual(estimate_available_new_courses(week_start), 2)

    def test_weekly_estimator_does_not_materialize_or_reserve_missing_sessions(self):
        course = self.make_course()
        dates = generate_treatment_dates(date(2026, 10, 5), total=29)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ])
        before_session_ids = set(
            TreatmentSession.objects.filter(treatment_course=course).values_list("pk", flat=True)
        )

        estimate_available_new_courses(date(2026, 10, 5))

        self.assertEqual(
            set(TreatmentSession.objects.filter(treatment_course=course).values_list("pk", flat=True)),
            before_session_ids,
        )
        self.assertFalse(RtmSTreatmentReservation.objects.filter(treatment_course=course).exists())

    def test_future_japanese_holidays_and_year_end_are_skipped(self):
        start_date = date(2027, 2, 10)
        dates = generate_treatment_dates(
            start_date,
            total=30,
            holidays=get_japanese_holidays(start_date, date(2028, 2, 10)),
        )
        self.assertEqual(len(dates), 30)
        self.assertNotIn(date(2027, 2, 11), dates)
        self.assertEqual(dates[0], start_date)

        year_end_dates = generate_treatment_dates(date(2026, 12, 28), total=5)
        self.assertEqual(year_end_dates[1], date(2027, 1, 4))

    def test_provisional_booking_creates_thirty_days_and_not_clinical_sessions(self):
        course = self.make_course()
        admission_capacity = RtmSAdmissionCapacity.objects.create(
            capacity=2, valid_from=date(2026, 10, 1), valid_to=date(2026, 10, 31),
        )
        pool = ResourcePool.objects.create(
            code="RESERVATION-ROOM",
            name="Reservation room",
            resource_type="rTMS_private",
            physical_capacity=3,
            operational_target=2,
        )
        other_course = self.make_course()
        assignment = ResourceAssignment.objects.create(
            treatment_course=other_course,
            resource_pool=pool,
            planned_start_date=date(2026, 10, 5),
            planned_end_date=date(2026, 10, 10),
        )

        result = self.book(course)

        self.assertEqual(len(result["reservations"]), 30)
        self.assertEqual(result["treatment_end_date"], result["reservations"][-1].treatment_date)
        self.assertEqual(result["reservations"][0].sequence_number, 1)
        self.assertEqual(result["reservations"][-1].sequence_number, 30)
        self.assertTrue(all(item.status == "provisional" for item in result["reservations"]))
        self.assertEqual(result["plan"].status, "provisional")
        self.assertEqual(result["plan"].planned_admission_date, date(2026, 10, 5))
        self.assertEqual(TreatmentSession.objects.count(), 0)
        self.assertEqual(RtmSAdmissionCapacity.objects.get(pk=admission_capacity.pk).capacity, 2)
        self.assertEqual(ResourceAssignment.objects.get(pk=assignment.pk).planned_end_date, date(2026, 10, 10))

    def test_calendar_adjustment_creates_provisional_reservations(self):
        course = self.make_course()
        entry = RtmSWaitlistEntry.objects.create(treatment_course=course, status="waiting")

        result = update_calendar_course_adjustment(
            treatment_course=course,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 5),
            private_room_planned=True,
            is_all_case_survey=False,
            waitlist_entry=entry,
        )

        self.assertEqual(len(result["reservation"]["reservations"]), 30)
        self.assertTrue(all(
            item.status == "provisional"
            for item in result["reservation"]["reservations"]
        ))
        entry.refresh_from_db()
        self.assertEqual(entry.status, "provisional")

    def test_inpatient_confirmation_creates_confirmed_reservations(self):
        course = self.make_course()
        pool = ResourcePool.objects.create(
            code="RESERVATION-CONFIRM-ROOM",
            name="Reservation confirmation room",
            resource_type="rTMS_private",
            physical_capacity=3,
            operational_target=2,
        )
        RtmSAdmissionCapacity.objects.create(
            capacity=1,
            valid_from=date(2026, 10, 1),
            valid_to=date(2026, 10, 31),
        )

        result = confirm_inpatient_schedule(
            treatment_course=course,
            resource_pool=pool,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5),
            room_end_date=date(2026, 10, 15),
        )

        reservations = RtmSTreatmentReservation.objects.filter(treatment_course=course)
        self.assertEqual(result["reservation"]["plan"].status, "scheduled")
        self.assertEqual(reservations.count(), 30)
        self.assertFalse(reservations.exclude(status="confirmed").exists())
        self.assertEqual(TreatmentSession.objects.filter(treatment_course=course).count(), 0)

    def test_first_treatment_date_change_updates_existing_reservations(self):
        course = self.make_course()
        self.book(course)
        TreatmentSession.objects.create(
            patient=course.patient,
            treatment_course=course,
            course_number=course.course_number,
            session_date=date(2026, 10, 5),
            status="planned",
        )

        reschedule_treatment_start_date(
            course.patient,
            date(2026, 10, 13),
            course_number=course.course_number,
        )

        first_reservation = RtmSTreatmentReservation.objects.get(
            treatment_course=course,
            sequence_number=1,
        )
        self.assertEqual(first_reservation.treatment_date, date(2026, 10, 13))

    def test_start_date_change_syncs_all_materialized_session_dates(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        old_dates = generate_treatment_dates(start_date, total=30)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in old_dates
        ])
        self.book(course, start_date=start_date)

        reschedule_treatment_start_date(
            course.patient,
            date(2026, 10, 13),
            course_number=course.course_number,
        )

        sessions = list(
            TreatmentSession.objects.filter(treatment_course=course)
            .order_by("session_date", "pk")
        )
        reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .order_by("sequence_number")
        )
        self.assertEqual(len(sessions), 30)
        self.assertEqual(len(reservations), 30)
        self.assertEqual(
            [session.session_date for session in sessions],
            [reservation.treatment_date for reservation in reservations],
        )
        stale_dates = set(old_dates) - {session.session_date for session in sessions}
        self.assertFalse(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=course,
                treatment_date__in=stale_dates,
                status__in=("provisional", "confirmed"),
            ).exists()
        )

    def test_session_sync_is_idempotent_and_does_not_create_inpatient_plan(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        course.admission_date = start_date
        course.first_treatment_date = start_date
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        dates = generate_treatment_dates(start_date, total=30)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ])
        course.refresh_from_db()
        original_updated_at = course.updated_at

        sync_rtms_treatment_reservation_from_sessions(treatment_course=course)
        first_reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .order_by("sequence_number")
        )
        first_ids = [reservation.pk for reservation in first_reservations]
        sync_rtms_treatment_reservation_from_sessions(treatment_course=course)

        second_reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .order_by("sequence_number")
        )
        course.refresh_from_db()
        self.assertEqual(len(first_reservations), 30)
        self.assertEqual(len(second_reservations), 30)
        self.assertEqual(first_ids, [reservation.pk for reservation in second_reservations])
        self.assertEqual(
            [reservation.sequence_number for reservation in second_reservations],
            list(range(1, 31)),
        )
        self.assertEqual(
            [reservation.treatment_date for reservation in second_reservations],
            dates,
        )
        self.assertTrue(all(reservation.status == "provisional" for reservation in second_reservations))
        self.assertTrue(all(reservation.treatment_course_id == course.pk for reservation in second_reservations))
        self.assertEqual(len({reservation.pk for reservation in second_reservations}), 30)
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=course).exists())
        self.assertEqual(course.updated_at, original_updated_at)

    def test_session_sync_does_not_create_reservations_before_thirty_sessions(self):
        course = self.make_course()
        dates = generate_treatment_dates(date(2026, 10, 5), total=29)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ])

        result = sync_rtms_treatment_reservation_from_sessions(treatment_course=course)

        self.assertIsNone(result)
        self.assertFalse(RtmSTreatmentReservation.objects.filter(treatment_course=course).exists())
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=course).exists())

    def test_canonical_fallback_does_not_create_missing_reservation_rows(self):
        course = self.make_course()
        course.admission_date = date(2026, 10, 5)
        course.first_treatment_date = date(2026, 10, 5)
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        first_reservation = RtmSTreatmentReservation.objects.create(
            treatment_course=course,
            treatment_date=date(2026, 10, 5),
            sequence_number=1,
            status="provisional",
            capacity_type="rTMS_private",
        )
        RtmSTreatmentReservation.objects.create(
            treatment_course=course,
            treatment_date=date(2026, 10, 6),
            sequence_number=2,
            status="provisional",
            capacity_type="rTMS_private",
        )
        TreatmentSession.objects.create(
            patient=course.patient,
            treatment_course=course,
            course_number=course.course_number,
            session_date=date(2026, 10, 7),
            status="planned",
        )

        sync_rtms_treatment_reservation_from_sessions(
            treatment_course=course,
            allow_canonical_fallback=True,
        )

        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(treatment_course=course).count(),
            2,
        )
        first_reservation.refresh_from_db()
        self.assertEqual(first_reservation.treatment_date, date(2026, 10, 7))

    def test_partial_session_sync_updates_only_matching_existing_reservations(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        self.book(course, start_date=start_date)
        dates = generate_treatment_dates(start_date, total=30)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates[:2]
        ])
        moved_date = date(2026, 12, 1)
        TreatmentSession.objects.filter(
            treatment_course=course, session_date=dates[1],
        ).update(session_date=moved_date)

        sync_rtms_treatment_reservation_from_sessions(treatment_course=course)

        reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .order_by("sequence_number")
        )
        self.assertEqual(len(reservations), 30)
        self.assertEqual(reservations[0].treatment_date, dates[0])
        self.assertEqual(reservations[1].treatment_date, moved_date)
        self.assertEqual(reservations[2].treatment_date, dates[2])
        self.assertEqual(
            [reservation.sequence_number for reservation in reservations],
            list(range(1, 31)),
        )

    def test_done_session_date_move_syncs_reservation(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        self.book(course, start_date=start_date)
        moved_session = TreatmentSession.objects.create(
            patient=course.patient,
            treatment_course=course,
            course_number=course.course_number,
            session_date=start_date,
            status="done",
        )
        user = get_user_model().objects.create_user(username="done-session-sync")
        self.client.force_login(user)

        response = self.client.post(
            reverse("rtms_app:clinical_path_reschedule", args=[course.patient_id]),
            data=json.dumps({
                "event_type": "treatment",
                "status": "done",
                "session_id": moved_session.pk,
                "course_number": course.course_number,
                "source_date": start_date.isoformat(),
                "target_date": "2026-12-01",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        moved_session.refresh_from_db()
        first_reservation = RtmSTreatmentReservation.objects.get(
            treatment_course=course, sequence_number=1,
        )
        self.assertEqual(moved_session.session_date, date(2026, 12, 1))
        self.assertEqual(first_reservation.treatment_date, moved_session.session_date)
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(treatment_course=course).count(),
            30,
        )

    def test_session_sync_preserves_cancelled_inpatient_plan_guard(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        course.admission_date = start_date
        course.first_treatment_date = start_date
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in generate_treatment_dates(start_date, total=30)
        ])
        plan = InpatientPlan.objects.create(treatment_course=course, status="cancelled")

        with self.assertRaises(InvalidTreatmentReservationError):
            sync_rtms_treatment_reservation_from_sessions(treatment_course=course)

        plan.refresh_from_db()
        self.assertEqual(plan.status, "cancelled")
        self.assertFalse(RtmSTreatmentReservation.objects.filter(treatment_course=course).exists())

    def test_cancelled_reservations_do_not_trigger_canonical_fallback(self):
        course = self.make_course()
        course.admission_date = date(2026, 10, 5)
        course.first_treatment_date = date(2026, 10, 5)
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        cancelled = RtmSTreatmentReservation.objects.create(
            treatment_course=course,
            treatment_date=date(2026, 10, 5),
            sequence_number=1,
            status="cancelled",
            capacity_type="rTMS_private",
        )

        result = sync_rtms_treatment_reservation_from_sessions(
            treatment_course=course,
            allow_canonical_fallback=True,
        )

        self.assertIsNone(result)
        self.assertEqual(
            list(RtmSTreatmentReservation.objects.filter(treatment_course=course).values_list("pk", "status")),
            [(cancelled.pk, "cancelled")],
        )

    def test_skip_reflow_cancels_skipped_session_reservation(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        course.admission_date = start_date
        course.first_treatment_date = start_date
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        dates = generate_treatment_dates(start_date, total=30)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="done" if index == 0 else "planned",
            )
            for index, treatment_date in enumerate(dates)
        ])
        sync_rtms_treatment_reservation_from_sessions(treatment_course=course)
        skipped_session = TreatmentSession.objects.get(
            treatment_course=course,
            session_date=dates[5],
        )
        skipped_session.status = "skipped"
        skipped_session.save(update_fields=["status"])

        shift_future_sessions(course.patient, skipped_session.session_date, course.course_number)

        sessions = list(
            TreatmentSession.objects.filter(treatment_course=course)
            .order_by("session_date", "pk")
        )
        reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .order_by("sequence_number")
        )
        skipped_reservation = next(
            reservation for reservation in reservations
            if reservation.treatment_date == skipped_session.session_date
        )
        self.assertEqual(len(reservations), 30)
        self.assertEqual(
            [session.session_date for session in sessions],
            [reservation.treatment_date for reservation in reservations],
        )
        self.assertEqual(skipped_reservation.status, "cancelled")
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=course,
                status__in=("provisional", "confirmed"),
            ).count(),
            29,
        )
        self.assertEqual(
            treatment_capacity_snapshot(skipped_session.session_date)["total_occupied"],
            0,
        )
        self.assertEqual(
            treatment_capacity_snapshot(dates[0])["total_occupied"],
            1,
        )
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=course).exists())

    def test_skipping_final_session_still_syncs_reservation(self):
        course = self.make_course()
        start_date = date(2026, 10, 5)
        course.admission_date = start_date
        course.first_treatment_date = start_date
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        dates = generate_treatment_dates(start_date, total=30)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=course.patient,
                treatment_course=course,
                course_number=course.course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ])
        self.book(course, start_date=start_date)
        final_session = TreatmentSession.objects.get(
            treatment_course=course, session_date=dates[-1],
        )
        final_session.status = "skipped"
        final_session.save(update_fields=["status"])

        shift_future_sessions(course.patient, final_session.session_date, course.course_number)

        final_reservation = RtmSTreatmentReservation.objects.get(
            treatment_course=course, sequence_number=30,
        )
        self.assertEqual(final_reservation.treatment_date, dates[-1])
        self.assertEqual(final_reservation.status, "cancelled")

    def test_clinical_path_moves_sync_and_replace_reservations_without_duplicates(self):
        course = self.make_course()
        course.admission_date = date(2026, 10, 5)
        course.first_treatment_date = date(2026, 10, 5)
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        user = get_user_model().objects.create_user(username="reservation-view-test")
        self.client.force_login(user)
        endpoint = reverse("rtms_app:clinical_path_reschedule", args=[course.patient_id])

        first_move = self.client.post(
            endpoint,
            data=json.dumps({
                "event_type": "treatment",
                "source_date": "2026-10-05",
                "target_date": "2026-10-06",
                "status": "planned",
                "course_number": course.course_number,
            }),
            content_type="application/json",
        )
        self.assertEqual(first_move.status_code, 200, first_move.content)
        self.assert_session_reservations_match(course)
        reservation_ids = dict(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .values_list("sequence_number", "pk")
        )

        second_session = TreatmentSession.objects.filter(
            treatment_course=course,
        ).order_by("session_date", "pk")[1]
        second_move = self.client.post(
            endpoint,
            data=json.dumps({
                "event_type": "treatment",
                "source_date": second_session.session_date.isoformat(),
                "target_date": "2026-10-08",
                "status": "planned",
                "session_id": second_session.pk,
                "course_number": course.course_number,
            }),
            content_type="application/json",
        )

        self.assertEqual(second_move.status_code, 200, second_move.content)
        self.assert_session_reservations_match(course)
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(treatment_course=course).count(),
            30,
        )
        self.assertEqual(
            dict(RtmSTreatmentReservation.objects.filter(treatment_course=course)
                 .values_list("sequence_number", "pk")),
            reservation_ids,
        )

    def test_course_two_session_sync_does_not_modify_course_one_reservations(self):
        course_one = self.make_course()
        self.book(course_one, start_date=date(2026, 10, 5))
        course_one_reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course_one)
            .order_by("sequence_number")
            .values_list("pk", "treatment_date", "capacity_type")
        )
        course_two = TreatmentCourse.objects.create(
            patient=course_one.patient,
            course_number=2,
            admission_date=date(2026, 10, 5),
            first_treatment_date=date(2026, 10, 5),
        )
        user = get_user_model().objects.create_user(username="reservation-course-two")
        self.client.force_login(user)

        response = self.client.post(
            reverse("rtms_app:clinical_path_reschedule", args=[course_one.patient_id]),
            data=json.dumps({
                "event_type": "treatment",
                "source_date": "2026-10-05",
                "target_date": "2026-10-06",
                "status": "planned",
                "course_number": course_two.course_number,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            list(
                RtmSTreatmentReservation.objects.filter(treatment_course=course_one)
                .order_by("sequence_number")
                .values_list("pk", "treatment_date", "capacity_type")
            ),
            course_one_reservations,
        )
        self.assert_session_reservations_match(course_two)
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(treatment_course=course_two).count(),
            30,
        )

    def assert_session_reservations_match(self, course):
        sessions = list(
            TreatmentSession.objects.filter(treatment_course=course)
            .order_by("session_date", "pk")
        )
        reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .order_by("sequence_number")
        )
        self.assertEqual(len(sessions), 30)
        self.assertEqual(len(reservations), 30)
        self.assertEqual(
            [session.session_date for session in sessions],
            [reservation.treatment_date for reservation in reservations],
        )

    def test_four_bookings_fill_three_private_and_one_general_slot(self):
        courses = [self.make_course() for _ in range(4)]
        for course in courses:
            self.book(course)

        snapshot = treatment_capacity_snapshot(date(2026, 10, 5))
        self.assertEqual(snapshot["rTMS_private"], {"capacity": 3, "occupied": 3, "remaining": 0})
        self.assertEqual(snapshot["general_ward"], {"capacity": 1, "occupied": 1, "remaining": 0})
        self.assertEqual(snapshot["total_capacity"], 4)
        self.assertEqual(snapshot["total_occupied"], 4)
        self.assertEqual(snapshot["total_remaining"], 0)

    def test_private_full_but_general_open_uses_general_capacity(self):
        courses = [self.make_course() for _ in range(4)]
        for course in courses[:3]:
            self.book(course)

        result = self.book(courses[3])
        self.assertTrue(all(item.capacity_type == "general_ward" for item in result["reservations"]))

    def test_general_full_but_private_open_uses_private_capacity(self):
        existing_course = self.make_course()
        dates = generate_treatment_dates(date(2026, 10, 5), total=30)
        RtmSTreatmentReservation.objects.bulk_create([
            RtmSTreatmentReservation(
                treatment_course=existing_course,
                treatment_date=treatment_date,
                sequence_number=index,
                status="provisional",
                capacity_type="general_ward",
            )
            for index, treatment_date in enumerate(dates, start=1)
        ])

        result = self.book(self.make_course())
        self.assertTrue(all(item.capacity_type == "rTMS_private" for item in result["reservations"]))

    def test_fifth_course_is_rejected_and_has_no_partial_writes(self):
        courses = [self.make_course() for _ in range(5)]
        for course in courses[:4]:
            self.book(course)

        with self.assertRaises(TreatmentReservationCapacityConflict) as raised:
            self.book(courses[4])

        self.assertEqual(len(raised.exception.conflicts), 30)
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(treatment_course=courses[4]).count(),
            0,
        )
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=courses[4]).exists())
        courses[4].refresh_from_db()
        self.assertIsNone(courses[4].admission_date)
        self.assertIsNone(courses[4].first_treatment_date)

    def test_one_overfull_date_rolls_back_entire_reservation(self):
        start_date = date(2026, 10, 5)
        dates = generate_treatment_dates(start_date, total=30)
        target_date = dates[12]
        for index, capacity_type in enumerate(
            ("rTMS_private", "rTMS_private", "rTMS_private", "general_ward"),
        ):
            course = self.make_course()
            RtmSTreatmentReservation.objects.create(
                treatment_course=course,
                treatment_date=target_date,
                sequence_number=1,
                status="confirmed",
                capacity_type=capacity_type,
            )

        target_course = self.make_course()
        with self.assertRaises(TreatmentReservationCapacityConflict) as raised:
            self.book(target_course, start_date)

        self.assertEqual([item["date"] for item in raised.exception.conflicts], [target_date])
        self.assertFalse(RtmSTreatmentReservation.objects.filter(treatment_course=target_course).exists())
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=target_course).exists())

    def test_waitlist_entry_moves_to_provisional_then_confirmed(self):
        course = self.make_course()
        entry = RtmSWaitlistEntry.objects.create(treatment_course=course, status="waiting")

        result = self.book(course, waitlist_entry=entry)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "provisional")
        self.assertEqual(entry.inpatient_plan_id, result["plan"].pk)

        confirmed = confirm_rtms_treatment_reservation(
            treatment_course=course,
            waitlist_entry=entry,
        )
        entry.refresh_from_db()
        self.assertEqual(entry.status, "scheduled")
        self.assertEqual(confirmed["plan"].status, "scheduled")
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=course, status="confirmed",
            ).count(),
            30,
        )

    def test_rescheduling_preserves_confirmation_and_clinical_sessions(self):
        course = self.make_course()
        original = self.book(course)
        confirm_rtms_treatment_reservation(treatment_course=course)
        clinical_session = TreatmentSession.objects.create(
            patient=course.patient,
            treatment_course=course,
            course_number=course.course_number,
            session_date=date(2026, 10, 5),
            status="done",
        )
        reservation_ids = {item.pk for item in original["reservations"]}

        updated = create_or_replace_rtms_treatment_reservation(
            treatment_course=course,
            admission_date=date(2026, 10, 13),
            first_treatment_date=date(2026, 10, 13),
        )

        self.assertEqual({item.pk for item in updated["reservations"]}, reservation_ids)
        self.assertTrue(all(item.status == "confirmed" for item in updated["reservations"]))
        self.assertEqual(updated["reservations"][0].treatment_date, date(2026, 10, 13))
        clinical_session.refresh_from_db()
        self.assertEqual(clinical_session.session_date, date(2026, 10, 5))
        self.assertEqual(clinical_session.status, "done")
        course.refresh_from_db()
        self.assertEqual(course.first_treatment_date, date(2026, 10, 13))

    def test_capacity_snapshot_can_exclude_course_for_reschedule_preview(self):
        course = self.make_course()
        result = self.book(course)

        snapshot = treatment_capacity_snapshot(date(2026, 10, 5), exclude_course_id=course.pk)
        self.assertEqual(snapshot["total_occupied"], 0)
        self.assertEqual(snapshot["total_remaining"], 4)
        preview = preview_rtms_treatment_reservation(
            first_treatment_date=date(2026, 10, 5),
            exclude_course_id=course.pk,
        )
        self.assertEqual(preview["treatment_end_date"], result["treatment_end_date"])
        self.assertEqual(preview["conflicts"], [])
        self.assertEqual(preview["capacity_snapshots"][0], snapshot)
        self.assertEqual(
            treatment_capacity_snapshots(
                preview["treatment_dates"][:2], exclude_course_id=course.pk,
            )[0],
            snapshot,
        )