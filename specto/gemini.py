"""A second model backend: Google Gemini, reached over plain HTTPS.

`GeminiCaller` fills the same `ModelCaller` contract as `ClaudeCaller`, so
every stage (read chunks, consolidate, resolve) works unchanged. It uses only
the standard library for HTTP, so there is no new dependency.

The one real piece of work here is `to_gemini_schema`: Gemini's structured
output takes an OpenAPI 3.0 subset, not the JSON Schema pydantic writes. The
converter inlines `$ref`s, drops what Gemini rejects (`title`, `default`,
`additionalProperties`), turns `Optional[X]` into X with `nullable: true`,
and adds `propertyOrdering` so field order stays stable.
"""
from __future__ import annotations

import copy
import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from specto.extract import USAGE_KEYS, ExtractionError

DEFAULT_MODEL = "gemini-2.5-pro"
DEFAULT_MAX_OUTPUT_TOKENS = 16384
DEFAULT_TIMEOUT = 600
TEMPERATURE = 0.2
KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ENABLE_API_HINT = (
    "Enable the Generative Language API in that Google Cloud project "
    "(APIs & Services > Library > Generative Language API > Enable), then retry."
)
RETRY_PREFIX = "Your previous answer was not valid for the schema: "
RETRY_SUFFIX = ". Answer again with valid JSON only."
VALIDATION_ERROR_CHARS = 300

# Schema keys Gemini accepts; everything else is dropped during conversion.
_KEPT_KEYS = ("type", "description", "properties", "required", "items", "enum", "format", "nullable")
_STRING_FORMATS = ("enum", "date-time")


class GeminiAPIError(ExtractionError):
    """The API said no (`status` is the HTTP code) or could not be reached (`status` is None)."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------ key


def gemini_api_key() -> Optional[str]:
    """The key from GEMINI_API_KEY, then GOOGLE_API_KEY; None when neither is set."""
    for name in KEY_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def gemini_available() -> bool:
    """True when a Gemini key is in the environment."""
    return gemini_api_key() is not None


# --------------------------------------------------------------------- schema


def to_gemini_schema(model: type[BaseModel]) -> dict:
    """Convert a pydantic model's JSON schema to the OpenAPI subset Gemini takes.

    Inlines `$ref`/`$defs`; drops `title`, `default`, `additionalProperties`
    and unsupported `format`s; turns `anyOf [X, null]` into X with
    `nullable: true`; keeps `enum` on strings only and every `required` list;
    adds `propertyOrdering` (declaration order) to every object.
    """
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    return _convert(schema, defs, stack=())


def _convert(node: dict, defs: dict, stack: tuple[str, ...]) -> dict:
    """One node of the pydantic schema -> one node of the Gemini schema."""
    node, stack = _inline_ref(node, defs, stack)

    if "anyOf" in node or "oneOf" in node:
        node, stack = _collapse_union(node, defs, stack)

    if "const" in node:
        node = {**node, "enum": [node["const"]]}
        node.pop("const")

    out: dict[str, Any] = {key: node[key] for key in _KEPT_KEYS if key in node}

    kind = out.get("type")
    if isinstance(kind, list):
        # ["string", "null"] style unions: the same thing as nullable.
        non_null = [t for t in kind if t != "null"]
        if "null" in kind:
            out["nullable"] = True
        out["type"] = kind = non_null[0] if len(non_null) == 1 else "string"

    if "enum" in out:
        if kind is None:
            out["type"] = kind = "string"
        if kind != "string":
            out.pop("enum")  # Gemini allows enum on strings only
        else:
            if any(v is None for v in out["enum"]):
                out["nullable"] = True
            out["enum"] = [str(v) for v in out["enum"] if v is not None]

    if "format" in out and (kind != "string" or out["format"] not in _STRING_FORMATS):
        out.pop("format")

    if kind == "object" or "properties" in out:
        out["type"] = "object"
        properties = out.get("properties", {})
        out["properties"] = {name: _convert(sub, defs, stack) for name, sub in properties.items()}
        out["propertyOrdering"] = list(properties.keys())
        if "required" in node:
            out["required"] = [name for name in node["required"] if name in properties]

    if kind == "array":
        items = out.get("items")
        if isinstance(items, dict):
            out["items"] = _convert(items, defs, stack)
        elif isinstance(items, list):  # tuple schemas: Gemini wants one item type
            out["items"] = _convert(items[0], defs, stack) if items else {"type": "string"}
        else:
            out["items"] = {"type": "string"}

    if kind == "null":
        out["type"] = "string"
        out["nullable"] = True

    return out


def _inline_ref(node: dict, defs: dict, stack: tuple[str, ...]) -> tuple[dict, tuple[str, ...]]:
    """Replace a `$ref` node with a copy of its definition (siblings kept)."""
    ref = node.get("$ref")
    if ref is None:
        return node, stack
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        raise ValueError(f"Cannot inline schema reference {ref!r}; only #/$defs/ refs are supported")
    name = ref[len(prefix):]
    if name not in defs:
        raise ValueError(f"Schema reference {ref!r} has no definition")
    if name in stack:
        raise ValueError(f"Schema for {name} refers to itself; Gemini cannot take recursive schemas")
    siblings = {k: v for k, v in node.items() if k != "$ref"}
    merged = {**copy.deepcopy(defs[name]), **siblings}
    return merged, stack + (name,)


def _collapse_union(node: dict, defs: dict, stack: tuple[str, ...]) -> tuple[dict, tuple[str, ...]]:
    """`anyOf [X, null]` -> X with nullable true. Real unions are refused."""
    options = node.get("anyOf") or node.get("oneOf") or []
    resolved = [_inline_ref(opt, defs, stack) for opt in options]
    real = [(opt, st) for opt, st in resolved if opt.get("type") != "null"]
    has_null = len(real) < len(resolved)
    if len(real) != 1:
        names = [opt.get("type", "?") for opt, _ in real]
        raise ValueError(
            f"Gemini schemas cannot express a union of {names}; "
            "make the field a single type (Optional is fine)"
        )
    chosen, chosen_stack = dict(real[0][0]), real[0][1]
    for key, value in node.items():
        if key not in ("anyOf", "oneOf", "default", "title"):
            chosen.setdefault(key, value)
    if has_null:
        chosen["nullable"] = True
    return chosen, chosen_stack


# --------------------------------------------------------------------- caller


class GeminiCaller:
    """Calls Gemini's generateContent with a JSON response schema."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        key = (api_key or "").strip() or gemini_api_key()
        if not key:
            raise ExtractionError(
                "No Gemini API key found. Set GEMINI_API_KEY (or GOOGLE_API_KEY) "
                "to a key from https://aistudio.google.com/apikey, or pass api_key=."
            )
        self.model = model
        self.api_key = key
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self.last_request_id: Optional[str] = None  # the reply's responseId for the latest call, if it sent one

    def __call__(
        self, system: str, content_blocks: list[dict], output_model: type[BaseModel]
    ) -> tuple[BaseModel, dict]:
        parts = [_to_part(block) for block in content_blocks]
        schema = to_gemini_schema(output_model)

        text, usage = self._generate(system, parts, schema)
        try:
            return output_model.model_validate_json(text), usage
        except ValidationError as first_error:
            hint = str(first_error)[:VALIDATION_ERROR_CHARS]
            retry_parts = parts + [{"text": f"{RETRY_PREFIX}{hint}{RETRY_SUFFIX}"}]
            text, retry_usage = self._generate(system, retry_parts, schema)
            usage = {key: usage[key] + retry_usage[key] for key in USAGE_KEYS}
            try:
                return output_model.model_validate_json(text), usage
            except ValidationError as second_error:
                raise ExtractionError(
                    f"Gemini's answer did not fit {output_model.__name__} even after one retry: "
                    f"{str(second_error)[:VALIDATION_ERROR_CHARS]}"
                ) from second_error

    # -- one request -----------------------------------------------------

    def _generate(self, system: str, parts: list[dict], schema: dict) -> tuple[str, dict]:
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "response_schema": schema,
                "temperature": TEMPERATURE,
                "max_output_tokens": self.max_output_tokens,
            },
        }
        response = self._post(body)
        return _text_of(response, self.max_output_tokens), _usage_of(response)

    def _post(self, body: dict) -> dict:
        url = ENDPOINT.format(model=self.model) + "?key=" + self.api_key
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                raw = reply.read()
        except urllib.error.HTTPError as error:
            raise GeminiAPIError(_http_error_message(error, self.model), error.code) from error
        except urllib.error.URLError as error:
            raise GeminiAPIError(f"Could not reach the Gemini API: {error.reason}") from error
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExtractionError(f"The Gemini API sent a reply that is not JSON: {error}") from error
        if isinstance(reply, dict) and reply.get("responseId"):
            self.last_request_id = str(reply["responseId"])
        return reply


# -------------------------------------------------------------------- helpers


def _to_part(block: dict) -> dict:
    kind = block.get("type")
    if kind == "text":
        return {"text": block["text"]}
    if kind == "image":
        source = block["source"]
        if source.get("type") != "base64":
            raise ValueError(f"Gemini backend only sends base64 images, not {source.get('type')!r}")
        return {"inline_data": {"mime_type": source["media_type"], "data": source["data"]}}
    raise ValueError(f"Unknown content block type {kind!r}")


def _http_error_message(error: urllib.error.HTTPError, model: str) -> str:
    try:
        raw = error.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - reading a closed error body
        raw = ""
    api_message = ""
    try:
        payload = json.loads(raw) if raw else {}
        api_message = str(payload.get("error", {}).get("message", "")) if isinstance(payload, dict) else ""
    except json.JSONDecodeError:
        api_message = raw.strip()[:500]
    message = f"Gemini API error {error.code} for {model}: {api_message or error.reason}"
    if error.code == 403 and "has not been used in project" in api_message:
        message += " " + ENABLE_API_HINT
    elif error.code in (401, 403) and not api_message:
        message += " Check that GEMINI_API_KEY is a valid key."
    elif error.code == 404:
        message += " Check the model name (for example gemini-2.5-pro or gemini-2.5-flash)."
    return message


def _text_of(response: dict, max_output_tokens: int) -> str:
    candidates = response.get("candidates") or []
    if not candidates:
        feedback = response.get("promptFeedback") or {}
        reason = feedback.get("blockReason")
        if reason:
            raise ExtractionError(f"Gemini blocked the request: {reason}")
        raise ExtractionError("Gemini returned no candidates.")
    candidate = candidates[0]
    finish = candidate.get("finishReason", "STOP")
    if finish == "MAX_TOKENS":
        raise ExtractionError(
            f"The answer was cut off at {max_output_tokens} tokens. "
            "Use fewer frames per call or raise max_output_tokens."
        )
    if finish not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
        raise ExtractionError(f"Gemini stopped early with reason {finish}.")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise ExtractionError("Gemini returned no structured output.")
    return text


def _usage_of(response: dict) -> dict:
    meta = response.get("usageMetadata") or {}
    return {
        "input_tokens": int(meta.get("promptTokenCount", 0) or 0),
        # Thinking tokens are billed as output, so they count here.
        "output_tokens": int(meta.get("candidatesTokenCount", 0) or 0)
        + int(meta.get("thoughtsTokenCount", 0) or 0),
        "cache_read_input_tokens": int(meta.get("cachedContentTokenCount", 0) or 0),
        "cache_creation_input_tokens": 0,
    }
