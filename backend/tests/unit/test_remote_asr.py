"""Remote (OpenAI-compatible) ASR backend. Hermetic: the HTTP layer is always patched."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from loguru import logger

from app.core.errors import MatchingError
from app.matcher import asr_models, asr_provider
from app.matcher.asr_models import (
    RemoteASRModel,
    clear_model_cache,
    create_asr_model,
    get_cached_model,
    model_output_key,
)
from app.matcher.asr_provider import NativeASRProvider, RemoteASRProvider, get_asr_provider
from app.matcher.models import Config
from app.models.app_config import AppConfig

BASE_URL = "http://x:9000/v1"
SECRET = "sk-super-secret-value"


class RemoteASRTestCase(unittest.TestCase):
    """Patches httpx.post onto a MockTransport so real requests get built and captured."""

    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(asr_models.REMOTE_API_KEY_ENV, None)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audio = Path(tmp.name) / "chunk.wav"
        self.audio.write_bytes(b"RIFFfakewav")

        self.requests: list[httpx.Request] = []

    def serve(self, *args, **kwargs):
        """Answer every call with this response; arguments as for ``httpx.Response``."""
        return self.serve_with(lambda request: httpx.Response(*args, **kwargs))

    def serve_with(self, handler):
        def capture(request):
            request.read()
            self.requests.append(request)
            return handler(request)

        transport = httpx.MockTransport(capture)

        def fake_post(url, **kwargs):
            with httpx.Client(transport=transport) as client:
                return client.post(url, **kwargs)

        patcher = patch.object(asr_models.httpx, "post", side_effect=fake_post)
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        return mock_post

    def model(self, **overrides):
        return create_asr_model({"type": "remote", "base_url": BASE_URL, **overrides})


class TestFactory(RemoteASRTestCase):
    def test_creates_remote_model(self):
        model = create_asr_model({"type": "remote", "base_url": BASE_URL, "name": "voxtral"})
        self.assertIsInstance(model, RemoteASRModel)
        self.assertEqual(model.device, "remote")
        self.assertEqual(model.model_name, "voxtral")

    def test_openai_compatible_is_an_alias(self):
        model = create_asr_model({"type": "openai-compatible", "base_url": BASE_URL})
        self.assertIsInstance(model, RemoteASRModel)

    def test_missing_base_url_raises_value_error(self):
        for config in ({"type": "remote"}, {"type": "remote", "base_url": "  "}):
            with self.assertRaisesRegex(ValueError, "remote ASR requires base_url"):
                create_asr_model(config)

    def test_model_name_defaults(self):
        self.assertEqual(self.model().model_name, "whisper-1")

    def test_load_marks_loaded_and_rejects_url_without_scheme(self):
        model = self.model()
        self.assertFalse(model.is_loaded)
        model.load()
        self.assertTrue(model.is_loaded)

        with self.assertRaisesRegex(ValueError, "http"):
            self.model(base_url="x:9000/v1").load()

    def test_url_forms_normalise_to_one_endpoint(self):
        for url, expected in (
            ("http://x:9000/v1", "http://x:9000/v1/audio/transcriptions"),
            ("http://x:9000/v1/", "http://x:9000/v1/audio/transcriptions"),
            ("http://x:9000", "http://x:9000/audio/transcriptions"),
            ("http://x:9000/", "http://x:9000/audio/transcriptions"),
        ):
            self.assertEqual(self.model(base_url=url).endpoint, expected)

    def test_inherits_match_score(self):
        self.assertIs(
            RemoteASRModel.calculate_match_score, asr_models.ASRModel.calculate_match_score
        )
        score = self.model().calculate_match_score("hello there world", "hello there world")
        self.assertGreater(score, 0.9)


class TestRequest(RemoteASRTestCase):
    def test_multipart_body(self):
        self.serve(200, json={"text": "hi"})
        self.model(name="voxtral", language="en").transcribe(self.audio)

        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "http://x:9000/v1/audio/transcriptions")
        self.assertTrue(request.headers["content-type"].startswith("multipart/form-data"))
        body = request.content.decode("latin-1")
        self.assertIn('name="model"\r\n\r\nvoxtral', body)
        self.assertIn('name="language"\r\n\r\nen', body)
        self.assertIn('name="response_format"\r\n\r\njson', body)
        self.assertIn('name="file"; filename="chunk.wav"', body)
        self.assertIn("Content-Type: audio/wav", body)
        self.assertIn("RIFFfakewav", body)

    def test_blank_language_is_omitted(self):
        self.serve(200, json={"text": "hi"})
        self.model(language="").transcribe(self.audio)
        self.assertNotIn('name="language"', self.requests[0].content.decode("latin-1"))

    def test_api_key_sent_as_bearer(self):
        self.serve(200, json={"text": "hi"})
        self.model(api_key=SECRET).transcribe(self.audio)
        self.assertEqual(self.requests[0].headers["authorization"], f"Bearer {SECRET}")

    def test_blank_api_key_omits_authorization_header(self):
        self.serve(200, json={"text": "hi"})
        self.model(api_key="").transcribe(self.audio)
        self.assertNotIn("authorization", self.requests[0].headers)

    def test_env_key_used_only_when_configured_key_is_blank(self):
        self.serve(200, json={"text": "hi"})
        os.environ[asr_models.REMOTE_API_KEY_ENV] = "from-env"

        self.model(api_key="").transcribe(self.audio)
        self.model(api_key="from-db").transcribe(self.audio)

        self.assertEqual(self.requests[0].headers["authorization"], "Bearer from-env")
        self.assertEqual(self.requests[1].headers["authorization"], "Bearer from-db")

    def test_timeouts_reach_the_client(self):
        mock_post = self.serve(200, json={"text": "hi"})
        self.model(timeout_connect=3.0, timeout_read=45.0).transcribe(self.audio)

        timeout = mock_post.call_args.kwargs["timeout"]
        self.assertEqual(timeout.connect, 3.0)
        self.assertEqual(timeout.read, 45.0)


class TestResponse(RemoteASRTestCase):
    def test_returns_text(self):
        self.serve(200, json={"text": "hello world"})
        self.assertEqual(self.model().transcribe(self.audio), {"text": "hello world"})

    def test_empty_transcript_is_empty_string_not_none(self):
        for payload in ({"text": ""}, {"text": None}):
            self.serve(200, json=payload)
            self.assertEqual(self.model().transcribe(self.audio), {"text": ""})

    def test_response_lacking_text_returns_empty(self):
        self.serve(200, json={"duration": 30.0})
        self.assertEqual(self.model().transcribe(self.audio), {"text": ""})

    def test_nested_text_path(self):
        self.serve(200, json={"data": {"transcript": "nested words"}})
        result = self.model(text_path="data.transcript").transcribe(self.audio)
        self.assertEqual(result, {"text": "nested words"})

    def test_unresolvable_text_path_names_top_level_keys(self):
        self.serve(200, json={"result": {"transcript": "x"}, "usage": {}})
        with self.assertRaises(MatchingError) as caught:
            self.model(text_path="data.transcript").transcribe(self.audio)
        self.assertIn("data.transcript", str(caught.exception))
        self.assertIn("result", str(caught.exception))
        self.assertIn("usage", str(caught.exception))

    def test_text_path_to_non_string_raises(self):
        self.serve(200, json={"data": {"transcript": "x"}})
        with self.assertRaisesRegex(MatchingError, "dict"):
            self.model(text_path="data").transcribe(self.audio)

    def test_plain_text_body_is_the_transcript(self):
        for body in ("plain words", "123"):
            self.serve(200, text=body, headers={"Content-Type": "text/plain; charset=utf-8"})
            self.assertEqual(self.model().transcribe(self.audio), {"text": body})

    def test_other_non_json_body_raises(self):
        self.serve(200, text="<html>", headers={"Content-Type": "text/html"})
        with self.assertRaisesRegex(MatchingError, "text/html"):
            self.model().transcribe(self.audio)

    def test_json_non_object_raises(self):
        self.serve(200, json=["a"])
        with self.assertRaises(MatchingError):
            self.model().transcribe(self.audio)


class TestErrors(RemoteASRTestCase):
    def assert_clean(self, message: str):
        self.assertNotIn(SECRET, message)
        self.assertNotIn("Bearer", message)

    def test_401_raises_without_key_or_bearer(self):
        self.serve(401, text=f"Bad credential: Bearer {SECRET} (also {SECRET})")
        with self.assertRaises(MatchingError) as caught:
            self.model(api_key=SECRET).transcribe(self.audio)
        self.assert_clean(str(caught.exception))
        self.assertIn("401", str(caught.exception))
        self.assertIn("Bad credential", str(caught.exception))

    def test_excerpt_is_bounded(self):
        self.serve(500, text="e" * 5000)
        with self.assertRaises(MatchingError) as caught:
            self.model().transcribe(self.audio)
        self.assertLess(len(str(caught.exception)), 300)

    def test_redirect_is_not_followed(self):
        self.serve(302, headers={"Location": "http://elsewhere/"})
        with self.assertRaisesRegex(MatchingError, "302"):
            self.model(api_key=SECRET).transcribe(self.audio)
        self.assertEqual(len(self.requests), 1)

    def test_transport_failures_raise_once_without_retry(self):
        for error in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow")):

            def fail(request, error=error):
                raise error

            mock_post = self.serve_with(fail)
            with self.assertRaisesRegex(MatchingError, type(error).__name__):
                self.model().transcribe(self.audio)
            self.assertEqual(mock_post.call_count, 1)

    def test_server_error_is_not_retried(self):
        mock_post = self.serve(503, text="busy")
        with self.assertRaises(MatchingError):
            self.model().transcribe(self.audio)
        self.assertEqual(mock_post.call_count, 1)

    def test_transport_error_quoting_the_header_is_redacted(self):
        def fail(request):
            raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {SECRET}'")

        self.serve_with(fail)
        with self.assertRaises(MatchingError) as caught:
            self.model(api_key=SECRET).transcribe(self.audio)
        self.assert_clean(str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_non_ascii_key_does_not_escape_as_unicode_error(self):
        key = "clé-secrète-value"
        self.serve(200, json={"text": "hi"})
        with self.assertRaises(MatchingError) as caught:
            self.model(api_key=key).transcribe(self.audio)
        self.assertNotIn(key, str(caught.exception))

    def test_diagnose_log_of_a_failure_never_prints_the_key(self):
        """The file sink runs with diagnose=True, which prints traceback variable values."""

        def fail(request):
            raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {SECRET}'")

        for arrange in (lambda: self.serve_with(fail), lambda: self.serve(401, text=SECRET)):
            arrange()
            # Build the model OUTSIDE the logged frame. diagnose=True prints the local
            # variables of every frame in the traceback, and an `api_key=SECRET` argument
            # in the calling frame is the caller's secret, not something this model can
            # redact. Keeping that frame out of the traceback is what makes this test
            # measure the model's own redaction rather than the test's own local.
            model = self.model(api_key=SECRET)
            sink = io.StringIO()
            sink_id = logger.add(sink, backtrace=True, diagnose=True, colorize=False)
            self.addCleanup(logger.remove, sink_id)
            try:
                model.transcribe(self.audio)
            except MatchingError as e:
                logger.opt(exception=e).error("transcription failed")
            self.assertIn("MatchingError", sink.getvalue())
            self.assert_clean(sink.getvalue())


class TestModelOutputKey(RemoteASRTestCase):
    def key(self, **overrides):
        return model_output_key({"type": "remote", "base_url": BASE_URL, **overrides})

    def test_differs_from_whisper_for_same_model_name(self):
        for device in ("cpu", "cuda", None):
            whisper = model_output_key({"type": "whisper", "name": "small", "device": device})
            self.assertNotEqual(self.key(name="small", device=device), whisper)

    def test_claims_no_local_compute_type(self):
        key = self.key(name="whisper-1")
        self.assertEqual(key, "remote_x:9000_whisper-1_auto")

    def test_differs_between_endpoints(self):
        self.assertNotEqual(
            self.key(base_url="http://a:9000/v1"), self.key(base_url="http://b:9000/v1")
        )

    def test_differs_between_services_on_one_gateway(self):
        self.assertNotEqual(
            self.key(base_url="http://gw/openai/v1"), self.key(base_url="http://gw/other/v1")
        )

    def test_differs_by_model_and_language(self):
        self.assertNotEqual(self.key(name="a"), self.key(name="b"))
        self.assertNotEqual(self.key(language="en"), self.key(language="fr"))

    def test_url_forms_of_one_endpoint_share_a_key(self):
        keys = {
            self.key(base_url=url)
            for url in ("http://x:9000/v1", "http://x:9000/v1/", "http://x:9000", "http://x:9000/")
        }
        self.assertEqual(len(keys), 1)

    def test_blank_name_keys_as_the_model_actually_sent(self):
        self.assertEqual(self.key(name=""), self.key(name="whisper-1"))

    def test_alias_and_case_share_the_remote_key_shape(self):
        for model_type in ("openai-compatible", "REMOTE"):
            self.assertEqual(self.key(type=model_type), self.key())

    def test_secrets_stay_out_of_the_persisted_key(self):
        key = self.key(base_url=f"http://user:{SECRET}@x:9000/v1", api_key=SECRET)
        self.assertNotIn(SECRET, key)


class TestGetCachedModel(RemoteASRTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(asr_models._model_cache, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_endpoints_never_share_a_model(self):
        first = get_cached_model({"type": "remote", "base_url": "http://a/v1", "name": "m"})
        second = get_cached_model({"type": "remote", "base_url": "http://b/v1", "name": "m"})
        self.assertEqual(first.base_url, "http://a/v1")
        self.assertEqual(second.base_url, "http://b/v1")

    def test_rotated_key_gets_a_new_model(self):
        config = {"type": "remote", "base_url": BASE_URL, "api_key": "old"}
        first = get_cached_model(config)
        self.assertIsNot(first, get_cached_model({**config, "api_key": "new"}))

    def test_same_config_reuses_a_loaded_model(self):
        config = {"type": "remote", "base_url": BASE_URL}
        first = get_cached_model(config)
        self.assertIs(first, get_cached_model(dict(config)))
        self.assertTrue(first.is_loaded)

    def test_api_key_is_not_in_the_cache_keys(self):
        get_cached_model({"type": "remote", "base_url": BASE_URL, "api_key": SECRET})
        self.assertNotIn(SECRET, " ".join(asr_models._model_cache))

    def test_transcribe_reloads_after_cache_clear(self):
        self.serve(200, json={"text": "hi"})
        model = get_cached_model({"type": "remote", "base_url": BASE_URL})
        clear_model_cache()
        self.assertFalse(model.is_loaded)
        self.assertEqual(model.transcribe(self.audio), {"text": "hi"})


class TestProvider(RemoteASRTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(asr_provider.factory_memo, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_remote_provider_transcribes_to_a_string(self):
        self.serve(200, json={"text": "hello world"})
        provider = get_asr_provider("remote", "m", remote_config={"base_url": BASE_URL})
        self.assertIsInstance(provider, RemoteASRProvider)
        provider.load()
        self.assertEqual(provider.transcribe(self.audio), "hello world")

    def test_remote_provider_without_base_url_raises(self):
        with self.assertRaisesRegex(ValueError, "remote ASR requires base_url"):
            get_asr_provider("remote", "m")

    def test_memo_separates_endpoints(self):
        endpoint_a = {"base_url": "http://a/v1"}
        first = get_asr_provider("remote", "m", remote_config=endpoint_a)
        second = get_asr_provider("remote", "m", remote_config={"base_url": "http://b/v1"})
        self.assertIsNot(first, second)
        self.assertIs(first, get_asr_provider("remote", "m", remote_config=dict(endpoint_a)))

    def test_whisper_ignores_remote_config(self):
        provider = get_asr_provider("whisper", "tiny", "cpu", remote_config={"base_url": BASE_URL})
        self.assertIs(type(provider), NativeASRProvider)
        self.assertIs(provider, get_asr_provider("whisper", "tiny", "cpu"))


class TestConfig(unittest.TestCase):
    def test_config_accepts_remote_and_stays_whisper_by_default(self):
        self.assertEqual(Config().asr_provider, "whisper")
        self.assertEqual(Config(asr_provider="remote").asr_provider, "remote")

    def test_parakeet_migration_intact(self):
        config = Config(asr_provider="parakeet", asr_model_name="x")
        self.assertEqual(config.asr_provider, "whisper")
        self.assertEqual(config.asr_model_name, "small")

    def test_config_repr_hides_the_key(self):
        self.assertNotIn(SECRET, repr(Config(asr_remote_api_key=SECRET)))

    def test_app_config_defaults(self):
        config = AppConfig()
        self.assertEqual(config.asr_remote_base_url, "")
        self.assertEqual(config.asr_remote_model, "whisper-1")
        self.assertEqual(config.asr_remote_language, "")
        self.assertEqual(config.asr_remote_api_key, "")
        self.assertEqual(config.asr_remote_text_path, "text")
        self.assertEqual(config.asr_remote_timeout_read, 120)

    def test_upgraded_rows_get_the_same_defaults_not_type_fallbacks(self):
        """_add_missing_columns backfills from these; an int column would otherwise get 0."""
        columns = AppConfig.__table__.c
        self.assertEqual(columns.asr_remote_timeout_read.server_default.arg.text, "120")
        self.assertEqual(columns.asr_remote_model.server_default.arg.text, "'whisper-1'")
        self.assertEqual(columns.asr_remote_text_path.server_default.arg.text, "'text'")


if __name__ == "__main__":
    unittest.main()
