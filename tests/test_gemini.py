"""The Gemini backend: schema conversion and the caller, with no network.

`urllib.request.urlopen` is replaced by a fake that records each request body
and answers with canned Gemini replies, so every path is exercised offline.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

import pytest
from pydantic import BaseModel

from specto import gemini as gemini_module
from specto.extract import (
    AnalysisResponse,
    ChunkReading,
    ExtractionError,
    RequirementsResponse,
    StructureResponse,
    USAGE_KEYS,
)
from specto.gemini import GeminiCaller, gemini_available, to_gemini_schema
from specto.resolve import ResolveResponse

RESPONSE_MODELS = [ChunkReading, AnalysisResponse, RequirementsResponse, StructureResponse, ResolveResponse]
FORBIDDEN_KEYS = ("$ref", "$defs", "additionalProperties", "title", "default", "anyOf", "oneOf", "const")


# ------------------------------------------------------------ schema helpers


def _walk(node, seen):
    """Every schema node; the keys of a `properties` map are field names, not keywords."""
    if isinstance(node, dict):
        seen.append(node)
        for key, value in node.items():
            if key == "properties":
                for sub in value.values():
                    _walk(sub, seen)
            else:
                _walk(value, seen)
    elif isinstance(node, list):
        for value in node:
            _walk(value, seen)


def _resolve(node: dict, defs: dict) -> dict:
    if "$ref" in node:
        return defs[node["$ref"].split("/")[-1]]
    return node


def _pairs(pydantic_node: dict, gemini_node: dict, defs: dict, out: list, seen: set) -> None:
    """Walk both trees together, yielding (pydantic object, gemini object) pairs."""
    pydantic_node = _resolve(pydantic_node, defs)
    if "anyOf" in pydantic_node:
        real = [o for o in pydantic_node["anyOf"] if o.get("type") != "null"]
        pydantic_node = {**pydantic_node, **_resolve(real[0], defs)}
    key = id(pydantic_node), id(gemini_node)
    if key in seen:
        return
    seen.add(key)
    out.append((pydantic_node, gemini_node))
    for name, sub in pydantic_node.get("properties", {}).items():
        _pairs(sub, gemini_node["properties"][name], defs, out, seen)
    if isinstance(pydantic_node.get("items"), dict):
        _pairs(pydantic_node["items"], gemini_node["items"], defs, out, seen)


# ------------------------------------------------------------ schema tests


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda m: m.__name__)
def test_gemini_schema_has_no_forbidden_keys(model):
    schema = to_gemini_schema(model)
    assert schema["type"] == "object"
    nodes: list[dict] = []
    _walk(schema, nodes)
    for node in nodes:
        for key in FORBIDDEN_KEYS:
            assert key not in node, f"{key} must not appear in a Gemini schema"
        if "enum" in node:
            assert node["type"] == "string"
            assert all(isinstance(v, str) for v in node["enum"])
        if "format" in node:
            assert node["type"] == "string" and node["format"] in ("enum", "date-time")
        if node.get("type") == "array":
            assert isinstance(node.get("items"), dict)


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda m: m.__name__)
def test_gemini_schema_keeps_order_required_enums_and_nullables(model):
    pydantic_schema = model.model_json_schema()
    defs = pydantic_schema.get("$defs", {})
    gemini_schema = to_gemini_schema(model)
    pairs: list = []
    _pairs(pydantic_schema, gemini_schema, defs, pairs, set())
    objects = 0
    optionals = 0
    for original, converted in pairs:
        if "properties" in original:
            objects += 1
            assert converted["type"] == "object"
            assert converted["propertyOrdering"] == list(original["properties"].keys())
            assert set(converted["properties"]) == set(original["properties"])
            assert converted.get("required", []) == original.get("required", [])
            for name, sub in original["properties"].items():
                if "anyOf" in sub and any(o.get("type") == "null" for o in sub["anyOf"]):
                    optionals += 1
                    assert converted["properties"][name].get("nullable") is True, name
                    assert "anyOf" not in converted["properties"][name]
        if "enum" in original:
            assert converted["enum"] == list(original["enum"])
        if "description" in original:
            assert converted["description"] == original["description"]
    assert objects >= 1
    if model is not RequirementsResponse:
        assert optionals >= 1, "these shapes have Optional fields; the walk must find them"


def test_gemini_schema_is_small_enough_and_keeps_prompt_hooks():
    text = json.dumps(to_gemini_schema(ChunkReading))
    assert len(text) < 60_000
    assert "keyframe_index" in text and "source_quote" in text


def test_optional_nested_model_and_odd_formats_convert():
    class Inner(BaseModel):
        name: str

    class Outer(BaseModel):
        inner: Optional[Inner] = None
        when: Optional[str] = None
        count: int = 0
        ids: list[int] = []

    schema = to_gemini_schema(Outer)
    inner = schema["properties"]["inner"]
    assert inner["type"] == "object" and inner["nullable"] is True
    assert inner["propertyOrdering"] == ["name"] and inner["required"] == ["name"]
    assert schema["properties"]["when"] == {"type": "string", "nullable": True}
    assert schema["properties"]["ids"] == {"type": "array", "items": {"type": "integer"}}
    assert schema.get("required", []) == []


def test_recursive_models_are_refused():
    class Node(BaseModel):
        children: list["Node"] = []

    with pytest.raises(ValueError, match="refers to itself"):
        to_gemini_schema(Node)


# ------------------------------------------------------------ fake transport


class FakeReply:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _reply(text: str, finish: str = "STOP", usage: Optional[dict] = None) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}, "finishReason": finish}],
        "usageMetadata": usage
        or {"promptTokenCount": 1200, "candidatesTokenCount": 50, "cachedContentTokenCount": 1000},
    }


class FakeUrlopen:
    """Records every request; answers from a queue of replies or errors."""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeReply(reply)

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].data.decode("utf-8"))


def _http_error(code: int, message: str) -> urllib.error.HTTPError:
    import io

    body = json.dumps({"error": {"code": code, "message": message, "status": "X"}}).encode("utf-8")
    return urllib.error.HTTPError("https://example/", code, "reason", {}, io.BytesIO(body))


CONTENT = [
    {"type": "text", "text": "Frame 0 at 00:00"},
    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
    {"type": "text", "text": "Transcript while frame 0 was showing: hello"},
]


@pytest.fixture
def no_env_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


# ------------------------------------------------------------ caller tests


def test_caller_sends_the_request_gemini_expects(monkeypatch: pytest.MonkeyPatch, no_env_key) -> None:
    fake = FakeUrlopen([_reply(json.dumps({"screens": [], "questions": []}))])
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    caller = GeminiCaller(api_key="k-123")
    parsed, usage = caller("SYSTEM", CONTENT, ChunkReading)

    assert isinstance(parsed, ChunkReading)
    assert parsed.screens == [] and parsed.fields == []
    assert usage == {
        "input_tokens": 1200,
        "output_tokens": 50,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 0,
    }
    assert set(usage) == set(USAGE_KEYS)
    assert caller.model == "gemini-2.5-pro"

    request = fake.requests[0]
    assert request.full_url == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key=k-123"
    )
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert fake.timeouts == [600]

    body = fake.body()
    assert body["system_instruction"] == {"parts": [{"text": "SYSTEM"}]}
    assert len(body["contents"]) == 1 and body["contents"][0]["role"] == "user"
    parts = body["contents"][0]["parts"]
    assert [list(p.keys())[0] for p in parts] == ["text", "inline_data", "text"]
    assert parts[0] == {"text": "Frame 0 at 00:00"}
    assert parts[1] == {"inline_data": {"mime_type": "image/jpeg", "data": "AAAA"}}
    assert parts[2]["text"].startswith("Transcript")

    config = body["generationConfig"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_schema"] == to_gemini_schema(ChunkReading)
    assert config["temperature"] == 0.2
    assert config["max_output_tokens"] == 16384


def test_caller_reads_key_from_env_in_order(monkeypatch: pytest.MonkeyPatch, no_env_key) -> None:
    assert gemini_available() is False
    monkeypatch.setenv("GOOGLE_API_KEY", "google")
    assert gemini_available() is True
    assert GeminiCaller().api_key == "google"
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    assert GeminiCaller().api_key == "gemini"
    assert GeminiCaller(api_key="explicit").api_key == "explicit"


def test_missing_key_is_a_clear_error_at_construction(no_env_key) -> None:
    with pytest.raises(ExtractionError, match="GEMINI_API_KEY"):
        GeminiCaller()


def test_max_tokens_raises_cut_off(monkeypatch: pytest.MonkeyPatch, no_env_key) -> None:
    fake = FakeUrlopen([_reply('{"screens": [', finish="MAX_TOKENS")])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    caller = GeminiCaller(api_key="k", max_output_tokens=2000)
    with pytest.raises(ExtractionError, match="cut off at 2000 tokens"):
        caller("SYSTEM", CONTENT, ChunkReading)
    assert len(fake.requests) == 1  # a cut-off answer is not retried here


@pytest.mark.parametrize("reason", ["SAFETY", "RECITATION"])
def test_safety_and_recitation_name_the_reason(monkeypatch, no_env_key, reason) -> None:
    fake = FakeUrlopen([_reply("", finish=reason)])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(ExtractionError, match=reason):
        GeminiCaller(api_key="k")("SYSTEM", CONTENT, ChunkReading)


def test_blocked_prompt_without_candidates(monkeypatch, no_env_key) -> None:
    fake = FakeUrlopen([{"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(ExtractionError, match="PROHIBITED_CONTENT"):
        GeminiCaller(api_key="k")("SYSTEM", CONTENT, ChunkReading)


def test_403_not_enabled_maps_to_enable_api_message(monkeypatch, no_env_key) -> None:
    message = (
        "Generative Language API has not been used in project 12345 before or it is disabled. "
        "Enable it by visiting https://console.developers.google.com/..."
    )
    fake = FakeUrlopen([_http_error(403, message)])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(ExtractionError) as info:
        GeminiCaller(api_key="k")("SYSTEM", CONTENT, ChunkReading)
    text = str(info.value)
    assert "403" in text and "has not been used in project 12345" in text
    assert "Enable the Generative Language API in that Google Cloud project" in text


def test_other_http_errors_carry_status_and_message(monkeypatch, no_env_key) -> None:
    fake = FakeUrlopen([_http_error(429, "Resource has been exhausted")])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(ExtractionError, match="429.*Resource has been exhausted"):
        GeminiCaller(api_key="k")("SYSTEM", CONTENT, ChunkReading)


def test_network_failure_is_an_extraction_error(monkeypatch, no_env_key) -> None:
    fake = FakeUrlopen([urllib.error.URLError("name resolution failed")])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(ExtractionError, match="Could not reach the Gemini API"):
        GeminiCaller(api_key="k")("SYSTEM", CONTENT, ChunkReading)


def test_invalid_json_is_retried_once_then_accepted(monkeypatch, no_env_key) -> None:
    good = json.dumps({"title": "T", "summary": "S"})
    fake = FakeUrlopen([
        _reply('{"title": 5, "nope"', usage={"promptTokenCount": 100, "candidatesTokenCount": 10}),
        _reply(good, usage={"promptTokenCount": 120, "candidatesTokenCount": 20, "cachedContentTokenCount": 5}),
    ])
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    parsed, usage = GeminiCaller(api_key="k")("SYSTEM", CONTENT, AnalysisResponse)

    assert isinstance(parsed, AnalysisResponse) and parsed.title == "T"
    assert len(fake.requests) == 2
    assert usage == {
        "input_tokens": 220,
        "output_tokens": 30,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
    }
    first, second = fake.body(0), fake.body(1)
    assert first["contents"][0]["parts"] == second["contents"][0]["parts"][:-1]
    extra = second["contents"][0]["parts"][-1]["text"]
    assert extra.startswith("Your previous answer was not valid for the schema: ")
    assert extra.endswith(". Answer again with valid JSON only.")
    assert len(extra) <= len("Your previous answer was not valid for the schema: ") + 300 + len(
        ". Answer again with valid JSON only."
    )
    assert second["generationConfig"]["response_schema"] == first["generationConfig"]["response_schema"]


def test_invalid_json_twice_raises_after_two_requests(monkeypatch, no_env_key) -> None:
    fake = FakeUrlopen([_reply("not json"), _reply('{"title": 1}')])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(ExtractionError, match="did not fit AnalysisResponse"):
        GeminiCaller(api_key="k")("SYSTEM", CONTENT, AnalysisResponse)
    assert len(fake.requests) == 2


def test_thinking_tokens_count_as_output_and_thought_parts_are_skipped(monkeypatch, no_env_key) -> None:
    payload = {
        "candidates": [{
            "content": {"parts": [{"text": "let me think", "thought": True}, {"text": "{"}, {"text": "}"}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 7},
    }
    fake = FakeUrlopen([payload])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    parsed, usage = GeminiCaller(api_key="k")("SYSTEM", CONTENT, ChunkReading)
    assert isinstance(parsed, ChunkReading)
    assert usage["output_tokens"] == 12 and usage["input_tokens"] == 10


def test_unknown_block_type_is_rejected_before_any_request(monkeypatch, no_env_key) -> None:
    fake = FakeUrlopen([])
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(ValueError, match="Unknown content block type"):
        GeminiCaller(api_key="k")("SYSTEM", [{"type": "document"}], ChunkReading)
    assert fake.requests == []


def test_gemini_prices_are_known_to_estimate(tmp_path) -> None:
    from specto.estimate import PRICES, estimate
    from specto.model import Recording

    assert PRICES["gemini-2.5-pro"] == (1.25, 10.0)
    assert PRICES["gemini-2.5-flash"] == (0.30, 2.50)
    rec = Recording(source="x.mp4", duration=0.0)
    for name in ("gemini-2.5-pro", "gemini-2.5-flash"):
        est = estimate(rec, tmp_path, model=name)
        assert not any("No price is known" in note for note in est.notes)
        assert est.model == name


def test_module_has_no_third_party_http_dependency() -> None:
    import inspect

    source = inspect.getsource(gemini_module)
    for name in ("requests", "httpx", "google.generativeai", "google.genai"):
        assert f"import {name}" not in source and f"from {name}" not in source
