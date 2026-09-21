"""`specto doctor --ping`: one tiny real call, checked entirely against stub clients (no network).

The Claude side swaps `anthropic.Anthropic` for a stub, the Gemini side swaps
`urllib.request.urlopen`, so the real `ClaudeCaller` and `GeminiCaller` run
their real code up to the wire.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import anthropic
import pytest

from specto import cli, ping
from specto.cli import main
from specto.extract import ClaudeCaller
from specto.gemini import GeminiCaller

try:
    import httpx2 as httpx_module  # what anthropic 1.5 builds its errors on
except ImportError:  # pragma: no cover - an older anthropic
    import httpx as httpx_module

CLAUDE_KEY = "sk-ant-api03-TOPSECRETVALUE0123456789"
GEMINI_KEY = "AIzaSyTOPSECRETGEMINIVALUE012345"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no specto.toml here unless a test writes one


def _claude_error(status: int, message: str) -> anthropic.APIStatusError:
    request = httpx_module.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx_module.Response(status, request=request)
    cls = {
        400: anthropic.BadRequestError, 401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
        404: anthropic.NotFoundError, 429: anthropic.RateLimitError, 500: anthropic.InternalServerError,
    }[status]
    return cls(message, response=response, body=None)


def _claude_client(monkeypatch: pytest.MonkeyPatch, result=None, raises: Exception | None = None) -> dict:
    """Swap the SDK client. `seen` records every request; a reply is `result`, or `raises` is raised."""
    seen: dict = {"calls": []}

    class Messages:
        def parse(self, **kwargs):
            seen["calls"].append(kwargs)
            if raises is not None:
                raise raises
            return result

    class Client:
        def __init__(self, *args, **kwargs):
            seen["constructed"] = True
            self.messages = Messages()

    monkeypatch.setattr(anthropic, "Anthropic", Client)
    return seen


def _claude_reply(request_id: str | None = "req_011TESTID", input_tokens: int = 40, output_tokens: int = 100):
    return SimpleNamespace(
        parsed_output=ping.Ping(ok=True, word="ready"),
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
        _request_id=request_id,
    )


class _FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _gemini_urlopen(monkeypatch: pytest.MonkeyPatch, reply) -> list:
    """Swap urlopen: `reply` is a dict to answer with, or an exception to raise."""
    requests: list = []

    def fake(request, timeout=None):
        requests.append(request)
        if isinstance(reply, Exception):
            raise reply
        return _FakeReply(json.dumps(reply).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return requests


def _gemini_reply() -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": '{"ok": true, "word": "ready"}'}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 8, "thoughtsTokenCount": 120},
        "responseId": "resp-abc123",
    }


def _gemini_http_error(status: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": status, "message": message}}).encode("utf-8")
    return urllib.error.HTTPError("https://example/?key=" + GEMINI_KEY, status, "reason", {}, io.BytesIO(body))


# ------------------------------------------------------------------ success


def test_claude_success_prints_the_whole_report(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    seen = _claude_client(monkeypatch, _claude_reply())
    assert main(["doctor", "--ping"]) == 0
    out, err = capsys.readouterr()
    assert "ping: claude / claude-opus-5" in out
    assert "request id   req_011TESTID" in out
    assert "40 in (what specto sent), 100 out" in out
    assert "about 0.27 cents" in out  # (40 * 5 + 100 * 25) / 1e6 dollars, in cents
    assert "time         " in out and " s\n" in out
    assert "ready: one real call went through the same code a run uses" in out
    assert err == ""
    assert len(seen["calls"]) == 1  # exactly one call


def test_the_tiny_call_goes_through_the_pipelines_caller(monkeypatch, capsys) -> None:
    """Same class as the pipeline, structured output, effort low, and a budget that thinking cannot starve."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    seen = _claude_client(monkeypatch, _claude_reply())
    built: list = []
    real_make_caller = cli.make_caller
    monkeypatch.setattr(cli, "make_caller", lambda *a, **k: built.append(real_make_caller(*a, **k)) or built[-1])
    assert main(["doctor", "--ping"]) == 0
    assert type(built[0]) is ClaudeCaller
    sent = seen["calls"][0]
    assert sent["output_format"] is ping.Ping
    assert sent["output_config"] == {"effort": "low"}
    assert sent["max_tokens"] == ping.PING_MAX_TOKENS >= 2000
    assert sent["model"] == "claude-opus-5"
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": ping.PING_PROMPT}]}]
    assert ClaudeCaller().max_tokens == 16000  # the pipeline's own default is unchanged


def test_gemini_success_uses_the_pipelines_gemini_caller(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    requests = _gemini_urlopen(monkeypatch, _gemini_reply())
    built: list = []
    real_make_caller = cli.make_caller
    monkeypatch.setattr(cli, "make_caller", lambda *a, **k: built.append(real_make_caller(*a, **k)) or built[-1])
    assert main(["doctor", "--ping", "--provider", "gemini"]) == 0
    out, err = capsys.readouterr()
    assert type(built[0]) is GeminiCaller
    assert len(requests) == 1
    body = json.loads(requests[0].data.decode("utf-8"))
    assert body["generationConfig"]["max_output_tokens"] == ping.PING_MAX_TOKENS
    assert body["generationConfig"]["response_schema"]["properties"].keys() == {"ok", "word"}
    assert "ping: gemini / gemini-2.5-pro" in out
    assert "request id   resp-abc123" in out
    assert "30 in (what specto sent), 128 out" in out  # answer 8 plus 120 thinking tokens
    assert "about 0.13 cents" in out  # (30 * 1.25 + 128 * 10) / 1e6 dollars, in cents
    assert "ready:" in out and err == ""


def test_a_model_without_a_price_says_so(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    _claude_client(monkeypatch, _claude_reply(request_id=None))
    assert main(["doctor", "--ping", "--model", "claude-brand-new-9"]) == 0
    out = capsys.readouterr().out
    assert "not estimated (specto has no price for claude-brand-new-9)" in out
    assert "request id   not reported by the service" in out


def test_a_tiny_cost_is_not_shown_as_zero(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    _claude_client(monkeypatch, _claude_reply(input_tokens=3, output_tokens=2))
    assert main(["doctor", "--ping"]) == 0
    assert "under 0.01 cents" in capsys.readouterr().out


# --------------------------------------------------------------- no key


@pytest.mark.parametrize("provider, name", [("claude", "ANTHROPIC_API_KEY"), ("gemini", "GEMINI_API_KEY")])
def test_no_key_exits_2_without_any_network_call(monkeypatch, capsys, provider, name) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("a network call was made")

    monkeypatch.setattr(anthropic, "Anthropic", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert main(["doctor", "--ping", "--provider", provider]) == 2
    out, err = capsys.readouterr()
    assert f"{name}" in err and "no call was made" in err and out == ""


def test_the_other_providers_key_does_not_count(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)  # a Gemini key, but Claude was asked for
    _claude_client(monkeypatch, raises=AssertionError("must not be called"))
    assert main(["doctor", "--ping"]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


# ---------------------------------------------------------- error mapping

CLAUDE_ERRORS = [
    (_claude_error(401, "invalid x-api-key"), "rejected ANTHROPIC_API_KEY as invalid (a 401 error)"),
    (_claude_error(404, "model: claude-opus-5"), "has no access to a model called claude-opus-5"),
    (_claude_error(403, "Request not allowed"), "not allowed to make this request (a 403 error)"),
    (
        _claude_error(400, "output_config.effort: not supported by this model"),
        "rejected the request (a 400 error); it says: output_config.effort: not supported by this model",
    ),
    (_claude_error(400, "Your credit balance is too low to access the API."), "no credit left"),
    (_claude_error(429, "rate limited"), "limiting this key (a 429 error)"),
    (_claude_error(500, "Internal server error"), "problem on its own side (a 500 error)"),
    (
        anthropic.APIConnectionError(request=httpx_module.Request("POST", "https://api.anthropic.com")),
        "could not reach the Claude service",
    ),
]


@pytest.mark.parametrize("error, sentence", CLAUDE_ERRORS, ids=lambda v: getattr(v, "status_code", None) or str(v)[:20])
def test_claude_errors_become_one_plain_sentence(monkeypatch, capsys, error, sentence) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    _claude_client(monkeypatch, raises=error)
    assert main(["doctor", "--ping"]) == 1
    out, err = capsys.readouterr()
    assert sentence in err
    assert out == ""
    assert "Traceback" not in err


GEMINI_ERRORS = [
    (_gemini_http_error(400, "API key not valid. Please pass a valid API key."), "rejected GEMINI_API_KEY as invalid"),
    (_gemini_http_error(401, "Request had invalid authentication credentials."), "rejected GEMINI_API_KEY as invalid"),
    (
        _gemini_http_error(403, "Generative Language API has not been used in project 123 before or it is disabled."),
        "Generative Language API is not switched on in the Google project",
    ),
    (_gemini_http_error(404, "models/gemini-2.5-pro is not found"), "has no access to a model called gemini-2.5-pro"),
    (_gemini_http_error(400, "Invalid JSON payload received."), "rejected the request (a 400 error); it says: "),
    (_gemini_http_error(429, "Resource has been exhausted"), "limiting this key (a 429 error)"),
    (urllib.error.URLError("name resolution failed"), "could not reach the Gemini service"),
]


@pytest.mark.parametrize("error, sentence", GEMINI_ERRORS, ids=lambda v: str(getattr(v, "code", None) or v)[:20])
def test_gemini_errors_become_one_plain_sentence(monkeypatch, capsys, error, sentence) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    _gemini_urlopen(monkeypatch, error)
    assert main(["doctor", "--ping", "--provider", "gemini"]) == 1
    out, err = capsys.readouterr()
    assert sentence in err
    assert out == ""


def test_the_403_message_names_the_console_step(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    _gemini_urlopen(monkeypatch, _gemini_http_error(403, "API has not been used in project 9 before or it is disabled"))
    assert main(["doctor", "--ping", "--provider", "gemini"]) == 1
    assert "APIs & Services > Library > Generative Language API, press Enable" in capsys.readouterr().err


def test_a_cut_off_answer_is_reported_not_hidden(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    cut = _claude_reply()
    cut.stop_reason, cut.parsed_output = "max_tokens", None
    _claude_client(monkeypatch, cut)
    assert main(["doctor", "--ping"]) == 1
    assert "cut off at 4000 tokens" in capsys.readouterr().err


# ----------------------------------------------------------- key never shown


def test_a_key_echoed_by_the_server_is_never_printed(monkeypatch, capsys) -> None:
    """Even when the server's own message repeats the key, no part of it reaches the output."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    _claude_client(monkeypatch, raises=_claude_error(400, f"bad request for key {CLAUDE_KEY} and sk-ant-other-abcdef123456"))
    assert main(["doctor", "--ping"]) == 1
    out, err = capsys.readouterr()
    for piece in (CLAUDE_KEY, CLAUDE_KEY[:12], CLAUDE_KEY[-8:], "sk-ant-other", "abcdef123456"):
        assert piece not in out + err
    assert "[key hidden]" in err


def test_the_gemini_key_in_the_url_never_reaches_the_output(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    for error in (
        _gemini_http_error(400, f"API key not valid: {GEMINI_KEY}"),
        _gemini_http_error(500, f"failed calling https://x/y?key={GEMINI_KEY}&alt=json"),
        urllib.error.URLError(f"timed out for https://x/y?key={GEMINI_KEY}"),
    ):
        _gemini_urlopen(monkeypatch, error)
        assert main(["doctor", "--ping", "--provider", "gemini"]) == 1
        out, err = capsys.readouterr()
        for piece in (GEMINI_KEY, GEMINI_KEY[:8], GEMINI_KEY[-8:]):
            assert piece not in out + err


def test_success_output_holds_no_key(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    _claude_client(monkeypatch, _claude_reply(request_id=f"req_{CLAUDE_KEY}"))  # even a hostile request id
    assert main(["doctor", "--ping"]) == 0
    out, err = capsys.readouterr()
    assert CLAUDE_KEY not in out + err and "sk-ant" not in out + err


# ---------------------------------------------------------------- config


def test_specto_toml_picks_the_provider_and_model(monkeypatch, capsys, tmp_path) -> None:
    (tmp_path / "specto.toml").write_text('[run]\nprovider = "gemini"\nmodel = "gemini-2.5-flash"\n', encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    requests = _gemini_urlopen(monkeypatch, _gemini_reply())
    assert main(["doctor", "--ping"]) == 0
    out = capsys.readouterr().out
    assert "config: specto.toml sets provider, model" in out
    assert "ping: gemini / gemini-2.5-flash" in out
    assert "gemini-2.5-flash:generateContent" in requests[0].full_url


def test_a_flag_beats_the_file(monkeypatch, capsys, tmp_path) -> None:
    (tmp_path / "specto.toml").write_text('[run]\nprovider = "gemini"\nmodel = "gemini-2.5-flash"\n', encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    seen = _claude_client(monkeypatch, _claude_reply())
    assert main(["doctor", "--ping", "--provider", "claude", "--model", "claude-sonnet-5"]) == 0
    assert seen["calls"][0]["model"] == "claude-sonnet-5"
    assert "ping: claude / claude-sonnet-5" in capsys.readouterr().out


def test_a_broken_settings_file_stops_the_ping(monkeypatch, capsys, tmp_path) -> None:
    (tmp_path / "specto.toml").write_text("[run\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", CLAUDE_KEY)
    _claude_client(monkeypatch, raises=AssertionError("must not be called"))
    assert main(["doctor", "--ping"]) == 2
    assert "specto.toml" in capsys.readouterr().err


def test_provider_or_model_without_ping_is_refused(capsys) -> None:
    assert main(["doctor", "--provider", "gemini"]) == 2
    assert "only apply together with --ping" in capsys.readouterr().err


def test_plain_doctor_is_unchanged(capsys) -> None:
    assert main(["doctor"]) in (0, 1)
    assert "ffmpeg" in capsys.readouterr().out
