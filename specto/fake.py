"""A stand-in for the Claude API so tests run with no key and no network.

`FakeCaller` looks at the request it is given and builds a small, plausible
answer of the shape asked for. It keeps every call it received so tests can
check what would have been sent to the model.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

FRAME_MARKER = re.compile(r"Frame (\d+) at (?:(\d+):)?(\d+):(\d+)")
KEYFRAME_IN_JSON = re.compile(r'"keyframe_index":\s*(\d+)')
TIMESTAMP_IN_JSON = re.compile(r'"timestamp":\s*([0-9.]+)')
REQUIREMENT_ID_IN_JSON = re.compile(r'"id":\s*"(R\d+)"')
QUESTION_ID_IN_JSON = re.compile(r'"id":\s*"(Q\d+)"')
ANSWER_IN_JSON = re.compile(r'"answer":\s*"((?:[^"\\]|\\.)*)"')
SCREEN_ID_IN_JSON = re.compile(r'"screen_id":\s*"(S\d+)"')


class FakeCaller:
    """Answers structured requests from the request text alone."""

    model = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, system: str, content_blocks: list[dict], output_model: type[BaseModel]
    ) -> tuple[BaseModel, dict]:
        self.calls.append(
            {"system": system, "content_blocks": content_blocks, "output_model": output_model}
        )
        text = "\n".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        fields = set(output_model.model_fields)
        if "requirement_candidates" in fields:
            data = self._chunk_reading(text)
        elif "follow_up_questions" in fields:
            data = self._resolve(text)
        else:
            data = self._analysis(text)
        data = {k: v for k, v in data.items() if k in fields}
        usage = {
            "input_tokens": 1000 + 100 * len(content_blocks),
            "output_tokens": 300,
            "cache_read_input_tokens": 0 if len(self.calls) == 1 else 800,
            "cache_creation_input_tokens": 800 if len(self.calls) == 1 else 0,
        }
        return output_model.model_validate(data), usage

    # ------------------------------------------------------------- pass 1 shape

    @staticmethod
    def _frames(text: str) -> list[tuple[int, float]]:
        """(keyframe_index, timestamp) for every 'Frame N at mm:ss' marker."""
        return [
            (int(i), int(h or 0) * 3600 + int(m) * 60 + int(s))
            for i, h, m, s in FRAME_MARKER.findall(text)
        ]

    def _chunk_reading(self, text: str) -> dict:
        frames = self._frames(text)
        if not frames:
            frames = [(0, 0.0)]
        first_index, first_time = frames[0]
        first_screen = f"S{first_index + 1:02d}"
        screens = [
            {
                "id": f"S{index + 1:02d}",
                "name": f"Screen at frame {index}",
                "purpose": f"Fake screen seen at frame {index}",
                "keyframe_indexes": [index],
                "first_seen": time,
                "field_ids": [],
                "action_ids": [],
            }
            for index, time in frames
        ]
        journey = [
            {
                "order": n,
                "screen_id": f"S{index + 1:02d}",
                "description": f"Fake step at frame {index}",
                "actor": "user",
                "timestamp": time,
                "keyframe_index": index,
            }
            for n, (index, time) in enumerate(frames, start=1)
        ]
        return {
            "screens": screens,
            "fields": [
                {
                    "id": f"F{first_index + 1:03d}",
                    "screen_id": first_screen,
                    "label": f"Field {first_index}",
                    "field_type": "text",
                    "required": None,
                    "example_value": None,
                    "source": "seen on screen",
                    "timestamp": first_time,
                    "keyframe_index": first_index,
                }
            ],
            "actions": [
                {
                    "id": f"A{first_index + 1:03d}",
                    "screen_id": first_screen,
                    "description": "Presses Save",
                    "control": "button",
                    "leads_to_screen_id": None,
                    "timestamp": first_time,
                    "keyframe_index": first_index,
                }
            ],
            "journey": journey,
            "requirement_candidates": [
                {
                    "id": f"R{first_index + 1:03d}",
                    "statement": f"The system must save what was entered at frame {first_index}.",
                    "source_quote": "we press save",
                    "timestamp": first_time,
                    "keyframe_index": first_index,
                    "screen_id": first_screen,
                    "kind": "functional",
                    "priority": "must",
                    "confidence": "high",
                }
            ],
            "questions": [
                {
                    "id": f"Q{first_index + 1:03d}",
                    "question": f"What happens if Save fails at frame {first_index}?",
                    "why_it_matters": "The developer needs the error path.",
                    "context_quote": "we press save",
                    "timestamp": first_time,
                    "keyframe_index": first_index,
                    "screen_id": first_screen,
                    "category": "edge case",
                }
            ],
        }

    # ------------------------------------------------------------- pass 2 shape

    def _analysis(self, text: str) -> dict:
        """A small Analysis whose frames come from the chunk notes in the request.

        Ids are deliberately non-contiguous so renumbering has something to do.
        """
        indexes = sorted({int(i) for i in KEYFRAME_IN_JSON.findall(text)}) or [0]
        times = sorted({float(t) for t in TIMESTAMP_IN_JSON.findall(text)}) or [0.0]
        k1, k2 = indexes[0], indexes[-1]
        t1, t2 = times[0], times[-1]
        return {
            "title": "Fake walkthrough",
            "summary": "A fake process. Someone types things. Then they press Save. Then it is saved.",
            "actors": ["Clerk", "Approver"],
            "screens": [
                {"id": "S03", "name": "Customer", "purpose": "Enter a customer", "keyframe_indexes": indexes[: max(1, len(indexes) // 2)], "first_seen": t1, "field_ids": ["F010"], "action_ids": ["A020"]},
                {"id": "S09", "name": "Approvals", "purpose": "Approve customers", "keyframe_indexes": indexes[len(indexes) // 2 :] or [k2], "first_seen": t2, "field_ids": [], "action_ids": []},
            ],
            "fields": [
                {"id": "F010", "screen_id": "S03", "label": "Postcode", "field_type": "text", "required": True, "example_value": None, "source": "both", "timestamp": t1, "keyframe_index": k1},
                {"id": "F030", "screen_id": "S09", "label": "Status", "field_type": "read-only", "required": None, "example_value": None, "source": "seen on screen", "timestamp": t2, "keyframe_index": k2},
            ],
            "actions": [
                {"id": "A020", "screen_id": "S03", "description": "Presses Save", "control": "button", "leads_to_screen_id": "S09", "timestamp": t1, "keyframe_index": k1},
            ],
            "journey": [
                {"order": 1, "screen_id": "S03", "description": "Clerk enters the customer", "actor": "Clerk", "timestamp": t1, "keyframe_index": k1},
                {"order": 2, "screen_id": "S09", "description": "Approver sees it in the queue", "actor": "Approver", "timestamp": t2, "keyframe_index": k2},
            ],
            "requirements": [
                {"id": "R010", "statement": "As a clerk, I need to save a customer, so that it reaches approvals.", "rationale": None, "source_quote": "we press save", "timestamp": t1, "keyframe_index": k1, "screen_id": "S03", "kind": "workflow", "priority": "must", "confidence": "high"},
                {"id": "R020", "statement": "The system must require a postcode before saving.", "rationale": None, "source_quote": "the postcode is mandatory", "timestamp": t1, "keyframe_index": k1, "screen_id": "S03", "kind": "validation", "priority": "must", "confidence": "medium"},
            ],
            "acceptance_criteria": [
                {"id": "AC005", "requirement_id": "R010", "given": "a filled-in customer form", "when": "the clerk presses Save", "then": "the customer appears in the approvals queue", "timestamp": t1, "keyframe_index": k1},
                {"id": "AC006", "requirement_id": "R010", "given": "the approvals queue is open", "when": "a customer is saved", "then": "its status reads Pending", "timestamp": t2, "keyframe_index": k2},
                {"id": "AC007", "requirement_id": "R020", "given": "a customer form with no postcode", "when": "the clerk presses Save", "then": "the form is not saved and the postcode is flagged", "timestamp": t1, "keyframe_index": k1},
            ],
            "questions": [
                {"id": "Q004", "question": "What happens if Save fails?", "why_it_matters": "The error path is not shown.", "context_quote": "we press save", "timestamp": t1, "keyframe_index": k1, "screen_id": "S03", "category": "edge case"},
                {"id": "Q008", "question": "Who is allowed to approve?", "why_it_matters": "Permissions decide who sees the queue.", "context_quote": None, "timestamp": t2, "keyframe_index": k2, "screen_id": "S09", "category": "permissions"},
            ],
        }

    # ------------------------------------------------------------ resolve shape

    def _resolve(self, text: str) -> dict:
        """One of each change, built from the first answered question in the
        request and the first existing requirement. Ids are temporary."""
        answers_json = text.split("Answered questions", 1)[-1]
        requirement_id = (REQUIREMENT_ID_IN_JSON.findall(text) or ["R001"])[0]
        question_match = QUESTION_ID_IN_JSON.search(answers_json)
        question_id = question_match.group(1) if question_match else "Q001"
        tail = answers_json[question_match.end():] if question_match else answers_json
        keyframe_match = KEYFRAME_IN_JSON.search(tail)
        timestamp_match = TIMESTAMP_IN_JSON.search(tail)
        screen_match = SCREEN_ID_IN_JSON.search(tail)
        answer_match = ANSWER_IN_JSON.search(tail)
        keyframe = int(keyframe_match.group(1)) if keyframe_match else 0
        timestamp = float(timestamp_match.group(1)) if timestamp_match else 0.0
        screen_id = screen_match.group(1) if screen_match else None
        answer = answer_match.group(1).replace('\\"', '"') if answer_match else "yes"
        return {
            "new_requirements": [
                {"id": "R901", "statement": f"The system must do what the expert's answer to {question_id} says.", "rationale": None, "source_quote": f"Answer to {question_id}: {answer}", "timestamp": timestamp, "keyframe_index": keyframe, "screen_id": screen_id, "kind": "functional", "priority": "must", "confidence": "high"},
            ],
            "updated_statements": [
                {"requirement_id": requirement_id, "statement": f"The system must do what {requirement_id} said, as narrowed by the answer to {question_id}.", "reason": f"{question_id} narrows it."},
            ],
            "new_acceptance_criteria": [
                {"id": "AC901", "requirement_id": "R901", "given": f"the situation described in the answer to {question_id}", "when": "the user does the step", "then": "the system behaves as the answer says", "timestamp": timestamp, "keyframe_index": keyframe},
            ],
            "follow_up_questions": [
                {"id": "Q901", "question": f"Does the answer to {question_id} hold for every role?", "why_it_matters": "The answer named one role only.", "context_quote": answer, "timestamp": timestamp, "keyframe_index": keyframe, "screen_id": screen_id, "category": "permissions", "status": "open"},
            ],
        }
