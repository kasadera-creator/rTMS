from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import Client, TestCase
from django.urls import reverse

from rtms_app.admin import rtms_admin_site
from rtms_app.models import (
    AuditLog,
    InpatientPlan,
    Patient,
    RtmSTreatmentReservation,
    RtmSWaitlistEntry,
    TreatmentCourse,
    TreatmentSession,
    TreatmentSkip,
)


class ProtectedAdminTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.staff = user_model.objects.create_user(
            username='readonly-admin-staff', is_staff=True,
        )
        self.superuser = user_model.objects.create_superuser(
            username='readonly-admin-superuser', password='test-password',
        )
        self.patient = Patient.objects.create(
            card_id='91234',
            name='Admin Protection Patient',
            birth_date=date(1980, 1, 1),
            attending_physician=self.staff,
            admission_date=date(2026, 8, 1),
            mapping_date=date(2026, 8, 2),
            first_treatment_date=date(2026, 8, 3),
        )
        self.course = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
        )
        self.session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course,
            course_number=1,
            session_date=date(2026, 8, 3),
            treatment_notes='Original session',
        )
        self.reservation = RtmSTreatmentReservation.objects.create(
            treatment_course=self.course,
            treatment_date=date(2026, 8, 3),
            sequence_number=1,
            status='provisional',
            capacity_type='general_ward',
        )
        self.plan = InpatientPlan.objects.create(treatment_course=self.course)
        self.waitlist = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course, status='waiting',
        )
        self.audit_log = AuditLog.objects.create(
            user=self.staff,
            patient=self.patient,
            target_model='TreatmentSession',
            target_pk=str(self.session.pk),
            action='UPDATE',
            summary='Original audit entry',
        )
        self.skip = TreatmentSkip.objects.create(
            treatment=self.session,
            action_type='postpone',
            effective_date=date(2026, 8, 3),
            performed_by=self.staff,
            snapshot={'session_date': '2026-08-03'},
        )
        self.protected_objects = (
            (TreatmentSession, self.session),
            (RtmSTreatmentReservation, self.reservation),
            (InpatientPlan, self.plan),
            (RtmSWaitlistEntry, self.waitlist),
            (AuditLog, self.audit_log),
            (TreatmentSkip, self.skip),
        )
        self._grant_all_model_permissions(self.staff)
        patient_content_type = ContentType.objects.get_for_model(Patient)
        patient_permissions = Permission.objects.filter(
            content_type=patient_content_type,
            codename__in=('view_patient', 'change_patient'),
        )
        self.staff.user_permissions.add(*patient_permissions)

    def _grant_all_model_permissions(self, user):
        permissions = []
        for model, _obj in self.protected_objects:
            content_type = ContentType.objects.get_for_model(model)
            permissions.extend(
                Permission.objects.filter(
                    content_type=content_type,
                    codename__in=[
                        f'{action}_{model._meta.model_name}'
                        for action in ('add', 'change', 'delete', 'view')
                    ],
                )
            )
        user.user_permissions.add(*permissions)

    def _admin_url(self, model, action, obj=None):
        route = f'rtms_admin:{model._meta.app_label}_{model._meta.model_name}_{action}'
        return reverse(route, args=[obj.pk] if obj is not None else None)

    def _snapshot(self, model, obj):
        return model.objects.filter(pk=obj.pk).values().get()

    def test_view_only_staff_can_list_but_cannot_write_protected_models(self):
        view_only_staff = get_user_model().objects.create_user(
            username='readonly-view-only-staff', is_staff=True,
        )
        view_permissions = []
        for model, _obj in self.protected_objects:
            content_type = ContentType.objects.get_for_model(model)
            view_permissions.append(
                Permission.objects.get(
                    content_type=content_type,
                    codename=f'view_{model._meta.model_name}',
                )
            )
        view_only_staff.user_permissions.add(*view_permissions)

        for model, _obj in self.protected_objects:
            for action in ('add', 'change', 'delete'):
                self.assertFalse(
                    view_only_staff.has_perm(
                        f'{model._meta.app_label}.{action}_{model._meta.model_name}'
                    )
                )

        client = Client()
        client.force_login(view_only_staff)

        for model, obj in self.protected_objects:
            with self.subTest(model=model.__name__):
                model_admin = rtms_admin_site._registry[model]
                before = self._snapshot(model, obj)
                changelist_url = self._admin_url(model, 'changelist')
                list_response = client.get(changelist_url, {'q': self.patient.name})

                self.assertTrue(
                    view_only_staff.has_perm(
                        f'{model._meta.app_label}.view_{model._meta.model_name}'
                    )
                )
                self.assertEqual(list_response.status_code, 200)
                self.assertEqual(model_admin.get_actions(list_response.wsgi_request), {})

                for action, target in (
                    ('add', None),
                    ('change', obj),
                    ('delete', obj),
                ):
                    url = self._admin_url(model, action, target)
                    self.assertEqual(client.get(url).status_code, 403)
                    self.assertEqual(client.post(url, {}).status_code, 403)

                bulk_delete = client.post(changelist_url, {
                    'action': 'delete_selected',
                    '_selected_action': str(obj.pk),
                    'index': '0',
                })
                self.assertEqual(bulk_delete.status_code, 200)
                self.assertEqual(self._snapshot(model, obj), before)

    def test_staff_and_superuser_can_list_but_cannot_write_protected_models(self):
        for user in (self.staff, self.superuser):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)

                for model, obj in self.protected_objects:
                    with self.subTest(model=model.__name__):
                        model_admin = rtms_admin_site._registry[model]
                        before = self._snapshot(model, obj)
                        changelist_url = self._admin_url(model, 'changelist')
                        list_response = client.get(changelist_url, {'q': self.patient.name})

                        self.assertEqual(list_response.status_code, 200)
                        self.assertEqual(model_admin.actions, None)
                        self.assertEqual(model_admin.list_display_links, ())
                        self.assertEqual(model_admin.get_actions(list_response.wsgi_request), {})

                        add_url = self._admin_url(model, 'add')
                        change_url = self._admin_url(model, 'change', obj)
                        delete_url = self._admin_url(model, 'delete', obj)
                        for url in (add_url, change_url, delete_url):
                            self.assertEqual(client.get(url).status_code, 403)
                            self.assertEqual(client.post(url, {}).status_code, 403)

                        bulk_delete = client.post(changelist_url, {
                            'action': 'delete_selected',
                            '_selected_action': str(obj.pk),
                            'index': '0',
                        })
                        self.assertEqual(bulk_delete.status_code, 200)
                        self.assertEqual(self._snapshot(model, obj), before)

    def test_protected_admin_list_search_and_filter_configuration_is_preserved(self):
        expected = {
            TreatmentSession: (('date', 'patient', 'performer'), ('date',), ()),
            RtmSTreatmentReservation: (
                ('treatment_course', 'sequence_number', 'treatment_date', 'status', 'capacity_type'),
                ('status', 'capacity_type', 'treatment_date'),
                ('treatment_course__patient__name', 'treatment_course__patient__card_id'),
            ),
            InpatientPlan: (
                ('treatment_course', 'status', 'planned_admission_date', 'planned_discharge_date', 'actual_admission_at', 'actual_discharge_at'),
                ('status', 'uncertainty', 'admission_type'),
                ('treatment_course__patient__name', 'treatment_course__patient__card_id'),
            ),
            RtmSWaitlistEntry: (
                ('treatment_course', 'status', 'priority', 'registered_at', 'preferred_start_note', 'scheduled_at', 'closed_at'),
                ('status', 'priority'),
                ('treatment_course__patient__name', 'treatment_course__patient__card_id'),
            ),
            AuditLog: (
                ('created_at', 'user', 'patient', 'action', 'target_model', 'target_pk', 'summary'),
                ('action', 'target_model', 'created_at'),
                ('user__username', 'patient__name', 'summary'),
            ),
            TreatmentSkip: (
                ('treatment', 'action_type', 'effective_date', 'performed_by', 'undone_by', 'undone_at', 'created_at'),
                ('action_type', 'created_at', 'undone_at'),
                ('treatment__patient__name', 'reason'),
            ),
        }
        for model, (list_display, list_filter, search_fields) in expected.items():
            model_admin = rtms_admin_site._registry[model]
            with self.subTest(model=model.__name__):
                self.assertEqual(model_admin.list_display, list_display)
                self.assertEqual(model_admin.list_filter, list_filter)
                self.assertEqual(model_admin.search_fields, search_fields)

    def test_patient_schedule_dates_are_readonly_but_other_fields_remain_editable(self):
        readonly_dates = ('admission_date', 'mapping_date', 'first_treatment_date')
        original_dates = tuple(getattr(self.patient, field) for field in readonly_dates)

        for user in (self.staff, self.superuser):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                change_url = self._admin_url(Patient, 'change', self.patient)
                response = client.get(change_url)

                self.assertEqual(response.status_code, 200)
                form = response.context['adminform'].form
                for field in readonly_dates:
                    self.assertNotIn(field, form.fields)
                    self.assertIn(field, response.context['adminform'].readonly_fields)
                self.assertIn('name', form.fields)

                data = {}
                for field_name, field in form.fields.items():
                    if not field.required:
                        continue
                    value = form[field_name].value()
                    if isinstance(value, date):
                        value = value.isoformat()
                    if value is None and field_name == 'attending_physician':
                        value = str(self.staff.pk)
                    data[field_name] = value
                data.update({
                    'name': f'Edited by {user.username}',
                    'admission_date': '2027-01-01',
                    'mapping_date': '2027-01-02',
                    'first_treatment_date': '2027-01-03',
                })

                post_response = client.post(change_url, data)
                self.assertEqual(post_response.status_code, 302)
                self.patient.refresh_from_db()
                self.assertEqual(self.patient.name, f'Edited by {user.username}')
                self.assertEqual(
                    tuple(getattr(self.patient, field) for field in readonly_dates),
                    original_dates,
                )