from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from provider import aplus


class AplusApiUrlTests(SimpleTestCase):
    @mock.patch("provider.aplus.config_loaders.provider_config")
    def test_build_api_url_uses_provider_host_for_relative_paths(self, provider_config):
        provider_config.return_value = {"host": "https://plus.example.com"}
        course = SimpleNamespace(provider="demo")

        self.assertEqual(
            aplus.build_api_url(course, "/api/v2/submissions/123/"),
            "https://plus.example.com/api/v2/submissions/123/",
        )
