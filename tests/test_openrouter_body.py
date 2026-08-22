"""OpenRouter response-body decoding and per-model timeouts (chief-wiggum#372).

Two runs against a slow model with a Mandarin prompt failed at the IDENTICAL
byte offset. Same offset twice is a deterministic parsing bug, not a network
blip: OpenRouter emits SSE-style keep-alive comment lines to hold the
connection open during long generations, and a bare `json.loads` over the whole
body chokes on them. A long non-English generation simply takes long enough to
collect them.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import consult_ai  # noqa: E402
from consult_ai import (  # noqa: E402
    OpenRouterBodyError,
    _body_excerpt,
    _decode_openrouter_body,
    tool_timeout,
)

PAYLOAD = {"choices": [{"message": {"content": "答案"}}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
BODY = json.dumps(PAYLOAD, ensure_ascii=False)
KEEPALIVE = ": OPENROUTER PROCESSING\n"


def _content(payload):
    return payload["choices"][0]["message"]["content"]


class TestBodyDecoding:
    def test_a_bare_json_body_still_parses(self):
        assert _content(_decode_openrouter_body(BODY)) == "答案"

    def test_keepalive_comment_lines_before_the_payload(self):
        """The #372 shape: a long generation collects keep-alives first."""
        assert _content(_decode_openrouter_body(KEEPALIVE * 1140 + BODY)) == "答案"

    def test_keepalives_on_both_sides(self):
        body = KEEPALIVE * 3 + "\n" + BODY + "\n" + KEEPALIVE * 2
        assert _content(_decode_openrouter_body(body)) == "答案"

    def test_an_sse_frame_stream(self):
        body = f"data: {BODY}\ndata: [DONE]\n"
        assert _content(_decode_openrouter_body(body)) == "答案"

    def test_sse_frames_interleaved_with_keepalives(self):
        body = f": ping\n\ndata: {BODY}\n\ndata: [DONE]"
        assert _content(_decode_openrouter_body(body)) == "答案"

    def test_the_last_real_frame_wins_over_done(self):
        first = json.dumps({"choices": [{"message": {"content": "first"}}]})
        second = json.dumps({"choices": [{"message": {"content": "second"}}]})
        body = f"data: {first}\ndata: {second}\ndata: [DONE]"
        assert _content(_decode_openrouter_body(body)) == "second"

    def test_multibyte_content_survives(self):
        """The failing prompt was Mandarin; a mangled decode would corrupt it."""
        payload = _decode_openrouter_body(KEEPALIVE + BODY)
        assert _content(payload) == "答案"
        assert payload["usage"]["prompt_tokens"] == 10


class TestTheHttpPathActuallyUsesIt:
    """The decoder is only a fix if the request path calls it.

    Testing `_decode_openrouter_body` alone left the call site unguarded: a
    mutation restoring the original bare `json.loads` there passed every test.
    That is the actual #372 bug, so it needs a test through the real path.
    """

    class _FakeResponse:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(self, body: str):
        encoded = body.encode("utf-8")

        def opener(request, timeout=None):
            return TestTheHttpPathActuallyUsesIt._FakeResponse(encoded)

        return opener

    def test_a_keepalive_prefixed_response_decodes_through_the_request_path(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            consult_ai.urllib.request, "urlopen", self._urlopen(KEEPALIVE * 1140 + BODY)
        )
        payload = consult_ai._http_json_with_deadline(object(), 5)
        assert _content(payload) == "答案"

    def test_a_bare_json_response_still_decodes_through_the_request_path(
        self, monkeypatch
    ):
        monkeypatch.setattr(consult_ai.urllib.request, "urlopen", self._urlopen(BODY))
        assert _content(consult_ai._http_json_with_deadline(object(), 5)) == "答案"

    def test_an_undecodable_response_raises_with_its_body(self, monkeypatch):
        monkeypatch.setattr(
            consult_ai.urllib.request, "urlopen", self._urlopen("<html>bad gateway</html>")
        )
        with pytest.raises(OpenRouterBodyError, match="bad gateway"):
            consult_ai._http_json_with_deadline(object(), 5)


class TestBodyFailuresAreDiagnosable:
    def test_an_empty_body_is_named_not_a_parse_offset(self):
        with pytest.raises(OpenRouterBodyError, match="empty body"):
            _decode_openrouter_body("   \n  ")

    def test_an_unparseable_body_carries_an_excerpt(self):
        """AC: capture the error body. 'Expecting value: line 1143 column 1' is
        not diagnosable on its own — that is why this bug took two runs to even
        characterise."""
        with pytest.raises(OpenRouterBodyError) as excinfo:
            _decode_openrouter_body("<html>gateway timeout</html>")
        message = str(excinfo.value)
        assert "gateway timeout" in message, "the raw body must reach the operator"

    def test_a_json_array_is_refused_by_type(self):
        with pytest.raises(OpenRouterBodyError, match="expected an object"):
            _decode_openrouter_body("[1, 2, 3]")

    def test_keepalives_alone_are_refused_rather_than_returning_nothing(self):
        with pytest.raises(OpenRouterBodyError):
            _decode_openrouter_body(KEEPALIVE * 50)

    def test_excerpts_are_bounded(self):
        assert len(_body_excerpt("x" * 10_000)) <= 420

    def test_excerpts_keep_both_ends(self):
        excerpt = _body_excerpt("HEAD" + "-" * 5000 + "TAIL")
        assert "HEAD" in excerpt and "TAIL" in excerpt


class TestPerModelTimeout:
    def test_a_slow_model_gets_its_configured_budget(self):
        """kimi is a REQUIRED divergence provider; the shared 300s failed the
        quorum every time it thought hard."""
        assert tool_timeout("openrouter", model="moonshotai/kimi-k3") == 900

    def test_other_models_keep_the_tool_default(self):
        assert tool_timeout("openrouter", model="deepseek/deepseek-v4-flash") == 300

    def test_an_unknown_model_keeps_the_tool_default(self):
        assert tool_timeout("openrouter", model="nobody/nothing") == 300

    def test_no_model_keeps_the_tool_default(self):
        assert tool_timeout("openrouter") == 300

    def test_an_explicit_override_still_wins(self):
        assert tool_timeout("openrouter", model="moonshotai/kimi-k3", override=60) == 60

    def test_the_env_override_still_beats_config(self, monkeypatch):
        """Documented precedence must not have shifted: env is the escape hatch."""
        monkeypatch.setenv("CW_CONSULT_TIMEOUT_OPENROUTER", "42")
        assert tool_timeout("openrouter", model="moonshotai/kimi-k3") == 42

    def test_a_malformed_config_value_degrades_rather_than_crashing(self, monkeypatch):
        monkeypatch.setattr(consult_ai, "_model_timeouts", lambda: {"m": "not-a-number"})
        assert tool_timeout("openrouter", model="m") == 300

    def test_the_shipped_config_declares_the_slow_model(self):
        config = json.loads((ROOT / "config" / "providers.json").read_text())
        assert config["providers"]["kimi"]["timeout_seconds"] >= 900
