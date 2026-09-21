"""A gateway does not have to accept the SDK's idea of authentication.

The OpenAI SDK sends `Authorization: Bearer` and nothing else. The gateway this
repository is pointed at is Azure API Management: measured on 2026-09-21, a
Bearer request returns 401 "Access denied due to missing subscription key",
and the identical request carrying Ocp-Apim-Subscription-Key returns 200. So
the client has to be able to send a header the SDK knows nothing about, or the
only way to reach that gateway is to install Node, a private wrapper repo, a
local MITM proxy, and a personal Anthropic login -- on a container shared with
every other bot on the account.
"""

import json
import os
import unittest
from unittest import mock

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from arxiv_assistant.utils.llm_client import get_openai_client, resolve_extra_headers


class ExtraHeaderTests(unittest.TestCase):
    def test_absent_means_no_headers(self):
        self.assertEqual(resolve_extra_headers(""), {})
        self.assertEqual(resolve_extra_headers("   "), {})

    def test_headers_are_parsed(self):
        got = resolve_extra_headers('{"Ocp-Apim-Subscription-Key": "k", "user": "u"}')
        self.assertEqual(got, {"Ocp-Apim-Subscription-Key": "k", "user": "u"})

    def test_malformed_json_raises_rather_than_sending_nothing(self):
        # Silently dropping them would look exactly like a wrong credential,
        # one 401 at a time, while the header is the entire reason the host can
        # reach a model.
        with self.assertRaises(ValueError):
            resolve_extra_headers('{"Ocp-Apim-Subscription-Key": ')

    def test_a_json_list_is_not_headers(self):
        with self.assertRaises(ValueError):
            resolve_extra_headers('["Ocp-Apim-Subscription-Key"]')

    def test_the_client_actually_carries_them(self):
        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with mock.patch.dict(os.environ, {
            "OPENAI_EXTRA_HEADERS": '{"Ocp-Apim-Subscription-Key": "k"}',
            "OPENAI_BASE_URL": "https://gw.example.invalid/v1",
        }), mock.patch.dict("sys.modules", {"openai": mock.Mock(OpenAI=FakeOpenAI)}):
            get_openai_client()

        self.assertEqual(captured.get("default_headers"),
                         {"Ocp-Apim-Subscription-Key": "k"},
                         "the headers were parsed and then not sent")

    def test_no_headers_means_no_default_headers_argument(self):
        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with mock.patch.dict(os.environ, {"OPENAI_EXTRA_HEADERS": ""}), \
             mock.patch.dict("sys.modules", {"openai": mock.Mock(OpenAI=FakeOpenAI)}):
            get_openai_client()

        self.assertIsNone(captured.get("default_headers"))

    def test_header_auth_does_not_require_an_api_key(self):
        # The SDK refuses to construct without one even when the gateway
        # ignores it. Demanding a real key the gateway never reads would make
        # this path unreachable for exactly the gateways it exists to serve.
        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env["OPENAI_EXTRA_HEADERS"] = '{"Ocp-Apim-Subscription-Key": "k"}'
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.dict("sys.modules", {"openai": mock.Mock(OpenAI=FakeOpenAI)}):
            get_openai_client()

        self.assertTrue(captured.get("api_key"),
                        "the SDK was handed an empty api_key and will refuse")
        self.assertEqual(captured.get("default_headers"),
                         {"Ocp-Apim-Subscription-Key": "k"})


class ModelOverrideTests(unittest.TestCase):
    """The model a host can reach is a property of the host, not of the repo."""

    def _config(self, model):
        import configparser

        c = configparser.ConfigParser()
        c.read_dict({"LLM": {"model": model}})
        return c

    def test_the_environment_beats_the_repository_file(self):
        # A deployment updates itself with `git reset --hard`, so a model edited
        # into configs/config.ini is gone on the next install. The gateway
        # answers a model it does not serve with "Deployment of ... is not
        # found", every call, forever.
        from arxiv_assistant.utils.llm_client import resolve_llm_model

        with mock.patch.dict(os.environ, {"ARXIV_ASSISTANT_LLM_MODEL": "gpt-6-astra"}):
            self.assertEqual(resolve_llm_model(self._config("gpt-5.6")), "gpt-6-astra")

    def test_an_explicit_override_still_wins(self):
        from arxiv_assistant.utils.llm_client import resolve_llm_model

        with mock.patch.dict(os.environ, {"ARXIV_ASSISTANT_LLM_MODEL": "gpt-6-astra"}):
            self.assertEqual(
                resolve_llm_model(self._config("gpt-5.6"), override="explicit"),
                "explicit")

    def test_without_the_variable_the_config_is_unchanged(self):
        from arxiv_assistant.utils.llm_client import resolve_llm_model

        env = {k: v for k, v in os.environ.items() if k != "ARXIV_ASSISTANT_LLM_MODEL"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(resolve_llm_model(self._config("gpt-5.6")), "gpt-5.6")

    def test_an_empty_variable_is_not_a_model(self):
        from arxiv_assistant.utils.llm_client import resolve_llm_model

        with mock.patch.dict(os.environ, {"ARXIV_ASSISTANT_LLM_MODEL": "  "}):
            self.assertEqual(resolve_llm_model(self._config("gpt-5.6")), "gpt-5.6")


class DetectionKnowsAboutHeaderAuthTests(unittest.TestCase):
    def test_a_header_authenticated_gateway_is_a_transport(self):
        import deploy.detect_target as dt

        self.assertEqual(
            dt.detect_backend(lambda name: None, {
                "OPENAI_BASE_URL": "https://gw.example.invalid/v1",
                "OPENAI_EXTRA_HEADERS": json.dumps({"Ocp-Apim-Subscription-Key": "k"}),
            }),
            "openai",
            "a host that reaches a model by header was told it cannot reach "
            "one, which blocks its own install",
        )

    def test_a_base_url_alone_is_still_nothing(self):
        import deploy.detect_target as dt

        self.assertEqual(
            dt.detect_backend(lambda name: None,
                              {"OPENAI_BASE_URL": "https://gw.example.invalid/v1"}),
            "none")


if __name__ == "__main__":
    unittest.main()
