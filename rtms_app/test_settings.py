import os
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.settings import is_production_environment
from config.settings.base import parse_comma_separated, resolve_secret_key


class SettingsConfigurationTests(SimpleTestCase):
    def test_default_hosts_include_all_supported_access_hosts(self):
        expected_hosts = {
            "rtms.lan",
            "seichiryo.jp",
            "192.168.100.50",
            "localhost",
            "127.0.0.1",
        }
        self.assertTrue(expected_hosts.issubset(settings.ALLOWED_HOSTS))

    def test_allowed_hosts_parser_uses_fallback_for_unset_and_empty_values(self):
        fallback = ("rtms.lan", "localhost")
        self.assertEqual(parse_comma_separated(None, fallback), list(fallback))
        self.assertEqual(parse_comma_separated("", fallback), list(fallback))

    def test_allowed_hosts_parser_trims_removes_empty_values_and_deduplicates(self):
        self.assertEqual(
            parse_comma_separated(" rtms.lan, ,localhost,rtms.lan, localhost ", ("fallback",)),
            ["rtms.lan", "localhost"],
        )

    def test_explicit_allowed_hosts_value_is_used(self):
        self.assertEqual(
            parse_comma_separated("custom.example, localhost", ("fallback",)),
            ["custom.example", "localhost"],
        )

    def test_development_secret_accepts_normal_value_and_falls_back_for_empty_values(self):
        with patch.dict(os.environ, {"DJANGO_SECRET_KEY": "development-value"}, clear=True):
            self.assertEqual(resolve_secret_key("fallback"), "development-value")
        with patch.dict(os.environ, {"DJANGO_SECRET_KEY": "", "SECRET_KEY": ""}, clear=True):
            self.assertEqual(resolve_secret_key("fallback"), "fallback")

    def test_production_secret_rejects_empty_values(self):
        with patch.dict(os.environ, {"DJANGO_SECRET_KEY": "", "SECRET_KEY": ""}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                resolve_secret_key(required=True)

    def test_production_secret_accepts_a_configured_value(self):
        with patch.dict(os.environ, {"DJANGO_SECRET_KEY": "production-value"}, clear=True):
            self.assertTrue(resolve_secret_key(required=True))

    def test_settings_selection_preserves_dev_and_prod_behavior(self):
        self.assertFalse(is_production_environment(None, None))
        self.assertFalse(is_production_environment("", None))
        self.assertTrue(is_production_environment("prod", None))
        self.assertRaises(ImproperlyConfigured, is_production_environment, "staging", None)

    def test_csrf_origins_are_explicit_scheme_based_values(self):
        self.assertEqual(settings.CSRF_TRUSTED_ORIGINS, ["https://seichiryo.jp"])
        self.assertNotIn("http://seichiryo.jp", settings.CSRF_TRUSTED_ORIGINS)

    def test_explicit_csrf_origins_preserve_only_configured_schemes(self):
        self.assertEqual(
            parse_comma_separated(
                "http://rtms.lan, https://192.168.100.50",
                ("https://seichiryo.jp",),
            ),
            ["http://rtms.lan", "https://192.168.100.50"],
        )