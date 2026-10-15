from datetime import date
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase

from rtms_app.middleware import RequestMiddleware
from rtms_app.models import AuditLog, Patient, TreatmentSession
from rtms_app.utils.request_context import _thread_locals, get_current_request


class RequestMiddlewareContextTests(SimpleTestCase):
    def setUp(self):
        self._clear_request_context()

    def tearDown(self):
        self._clear_request_context()
        super().tearDown()

    @staticmethod
    def _clear_request_context():
        if hasattr(_thread_locals, 'request'):
            delattr(_thread_locals, 'request')

    def _request(self, path, username='request-user'):
        request = RequestFactory().get(path)
        request.user = SimpleNamespace(username=username)
        return request

    def test_normal_request_clears_context_after_response(self):
        request = self._request('/normal/')
        observed = []
        response = HttpResponse('ok')

        result = RequestMiddleware(
            lambda current: (observed.append(get_current_request()), response)[1]
        )(request)

        self.assertIs(observed[0], request)
        self.assertIs(result, response)
        self.assertEqual(result['X-Request-ID'], request.request_id)
        self.assertIsNone(get_current_request())

    def test_exception_restores_existing_context(self):
        previous_request = object()
        _thread_locals.request = previous_request
        request = self._request('/raises/')

        def raise_error(current):
            self.assertIs(get_current_request(), current)
            raise RuntimeError('request failed')

        with self.assertRaisesRegex(RuntimeError, 'request failed'):
            RequestMiddleware(raise_error)(request)

        self.assertIs(get_current_request(), previous_request)

    def test_nested_request_restores_outer_context(self):
        previous_request = object()
        _thread_locals.request = previous_request
        outer_request = self._request('/outer/', username='outer-user')
        inner_request = self._request('/inner/', username='inner-user')

        def inner_response(current):
            self.assertIs(get_current_request(), current)
            return HttpResponse('inner')

        inner_middleware = RequestMiddleware(inner_response)

        def outer_response(current):
            self.assertIs(get_current_request(), current)
            inner_middleware(inner_request)
            self.assertIs(get_current_request(), current)
            return HttpResponse('outer')

        response = RequestMiddleware(outer_response)(outer_request)

        self.assertEqual(response.content, b'outer')
        self.assertIs(get_current_request(), previous_request)

    def test_consecutive_requests_do_not_share_users(self):
        observed_users = []

        for username in ('first-user', 'second-user'):
            request = self._request(f'/{username}/', username=username)
            middleware = RequestMiddleware(
                lambda current: (
                    observed_users.append(
                        (get_current_request(), current.user.username)
                    ),
                    HttpResponse('ok'),
                )[1]
            )
            middleware(request)
            self.assertIsNone(get_current_request())

        self.assertEqual(
            [username for _, username in observed_users],
            ['first-user', 'second-user'],
        )
        self.assertEqual(observed_users[0][0].user.username, 'first-user')
        self.assertEqual(observed_users[1][0].user.username, 'second-user')
        self.assertIsNot(observed_users[0][0], observed_users[1][0])


class RequestMiddlewareAuditLogTests(TransactionTestCase):
    def test_session_save_after_request_does_not_use_deleted_actor(self):
        self.assertIsNone(get_current_request())
        actor = get_user_model().objects.create_user(username='request-context-actor')
        patient = Patient.objects.create(
            card_id='CTX01',
            name='Request Context Patient',
            birth_date=date(1980, 1, 1),
        )
        request = RequestFactory().get('/audit-context/')
        request.user = actor

        RequestMiddleware(lambda current: HttpResponse('ok'))(request)
        self.assertIsNone(get_current_request())

        actor.delete()
        session = TreatmentSession.objects.create(
            patient=patient,
            session_date=date(2026, 9, 7),
        )

        self.assertFalse(AuditLog.objects.filter(
            target_model='TreatmentSession', target_pk=str(session.pk),
        ).exists())