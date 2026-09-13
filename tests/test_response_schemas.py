"""Every shape we ask the model to fill must pass the SDK's structured-output
schema rules, or the first real run would fail on the request itself. This
runs the SDK's own converter, the one messages.parse uses, with no network."""
from __future__ import annotations

import json

import pytest
from anthropic.lib._parse._transform import transform_schema

from specto.extract import AnalysisResponse, ChunkReading, RequirementsResponse, StructureResponse
from specto.resolve import ResolveResponse

RESPONSE_MODELS = [ChunkReading, AnalysisResponse, RequirementsResponse, StructureResponse, ResolveResponse]


def _walk(node, seen):
    if isinstance(node, dict):
        seen.append(node)
        for value in node.values():
            _walk(value, seen)
    elif isinstance(node, list):
        for value in node:
            _walk(value, seen)


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda m: m.__name__)
def test_response_model_converts_to_an_api_schema(model):
    schema = transform_schema(model)
    assert schema["type"] == "object"
    nodes: list[dict] = []
    _walk(schema, nodes)
    for node in nodes:
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, "objects must be closed for structured output"
            assert set(node.get("required", [])) <= set(node["properties"]), "required names must exist"
        for key in ("minimum", "maximum", "minLength", "maxLength", "pattern", "minItems", "maxItems"):
            assert key not in node, f"{key} is not allowed in a structured-output schema"
    text = json.dumps(schema)
    assert len(text) < 60_000, f"{model.__name__} schema is {len(text)} bytes; the API caps schema size"


def test_schema_keeps_the_field_descriptions_the_prompt_relies_on():
    schema = transform_schema(ChunkReading)
    text = json.dumps(schema)
    assert "keyframe_index" in text and "source_quote" in text
    assert "Never invent" in text or "never invent" in text
