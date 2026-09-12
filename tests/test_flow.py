"""The screen-flow picture: screens as boxes, actions as arrows, in journey order."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from specto.flow import MARGIN, TOP, Flow, FlowEdge, FlowNode, build_flow, flow_markdown, flow_mermaid, flow_svg
from specto.model import Action, Analysis, JourneyStep, Screen

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


@pytest.fixture
def flow(analysis: Analysis) -> Flow:
    return build_flow(analysis)


def _screen(sid: str, name: str, first_seen: float = 0.0) -> Screen:
    return Screen(id=sid, name=name, purpose="x", first_seen=first_seen)


def _step(order: int, sid: str) -> JourneyStep:
    return JourneyStep(order=order, screen_id=sid, description="x", timestamp=float(order), keyframe_index=0)


def _action(aid: str, sid: str, desc: str, leads_to: str | None) -> Action:
    return Action(id=aid, screen_id=sid, description=desc, leads_to_screen_id=leads_to, timestamp=1.0, keyframe_index=0)


# ------------------------------------------------------------------ build_flow


def test_one_node_per_screen_in_journey_order(analysis: Analysis, flow: Flow) -> None:
    assert len(flow.nodes) == len(analysis.screens)
    assert [n.id for n in flow.nodes] == ["S01", "S02", "S03", "S04"]
    assert [n.order for n in flow.nodes] == [1, 2, 3, 4]
    by_id = {n.id: n for n in flow.nodes}
    assert by_id["S02"].label == "Customer details"
    assert by_id["S02"].n_fields == 5 and by_id["S02"].n_actions == 1
    assert by_id["S03"].n_fields == 2 and by_id["S03"].n_actions == 2


def test_order_follows_journey_not_screen_list() -> None:
    a = Analysis(title="t", summary="s", screens=[_screen("S01", "Home"), _screen("S02", "Detail"), _screen("S03", "Never")],
                 journey=[_step(1, "S02"), _step(2, "S01"), _step(3, "S02")])
    flow = build_flow(a)
    assert [(n.id, n.order) for n in flow.nodes] == [("S02", 1), ("S01", 2), ("S03", 3)]


def test_action_with_leads_to_becomes_action_edge(flow: Flow) -> None:
    assert FlowEdge(from_id="S01", to_id="S02", label="Presses New customer", kind="action") in flow.edges
    assert FlowEdge(from_id="S02", to_id="S03", label="Presses Save", kind="action") in flow.edges
    assert FlowEdge(from_id="S03", to_id="S04", label="Presses Submit", kind="action") in flow.edges
    # Actions without a destination (upload, approve) draw nothing.
    assert not any(e.label in ("Uploads the ID scan", "Presses Approve") for e in flow.edges)


def test_fixture_has_no_journey_edges_where_an_action_already_joins_the_pair(flow: Flow) -> None:
    assert [e.kind for e in flow.edges] == ["action", "action", "action"]


def test_consecutive_journey_screens_without_an_action_become_a_journey_edge() -> None:
    a = Analysis(title="t", summary="s", screens=[_screen("S01", "A"), _screen("S02", "B"), _screen("S03", "C")],
                 actions=[_action("A001", "S01", "Presses Go", "S02")],
                 journey=[_step(1, "S01"), _step(2, "S02"), _step(3, "S02"), _step(4, "S03"), _step(5, "S01")])
    flow = build_flow(a)
    assert FlowEdge(from_id="S02", to_id="S03", label="", kind="journey") in flow.edges
    assert FlowEdge(from_id="S03", to_id="S01", label="", kind="journey") in flow.edges
    # S01 -> S02 is covered by the action, so no journey edge is added for it.
    assert not any(e.from_id == "S01" and e.to_id == "S02" and e.kind == "journey" for e in flow.edges)


def test_no_duplicate_edges_and_no_self_loops() -> None:
    a = Analysis(title="t", summary="s", screens=[_screen("S01", "A"), _screen("S02", "B")],
                 actions=[_action("A001", "S01", "Presses Go", "S02"), _action("A002", "S01", "Presses Go", "S02"),
                          _action("A003", "S01", "Refreshes", "S01")],
                 journey=[_step(1, "S01"), _step(2, "S02"), _step(3, "S01"), _step(4, "S02"), _step(5, "S02")])
    flow = build_flow(a)
    assert len(flow.edges) == len({(e.from_id, e.to_id, e.label, e.kind) for e in flow.edges})
    assert not any(e.from_id == e.to_id for e in flow.edges)
    assert sum(1 for e in flow.edges if e.kind == "action") == 1
    assert sum(1 for e in flow.edges if e.kind == "journey") == 1  # S02 -> S01, once


def test_edges_to_unknown_screens_are_dropped() -> None:
    a = Analysis(title="t", summary="s", screens=[_screen("S01", "A")],
                 actions=[_action("A001", "S01", "Presses Go", "S99")])
    assert build_flow(a).edges == []


# ------------------------------------------------------------------ mermaid


def test_mermaid_has_the_header_every_node_and_the_edges(flow: Flow) -> None:
    text = flow_mermaid(flow)
    assert text.startswith("```mermaid\nflowchart LR")
    assert text.endswith("```")
    for n in flow.nodes:
        assert f'{n.id}["{n.label}"]' in text
    assert 'S01 -- "Presses New customer" --> S02' in text
    assert 'S02 -- "Presses Save" --> S03' in text


def test_mermaid_escapes_quotes_and_brackets_and_draws_journey_edges_plain() -> None:
    a = Analysis(title="t", summary="s", screens=[_screen("S01", 'Say "hi" [now]'), _screen("S02", "B")],
                 journey=[_step(1, "S01"), _step(2, "S02")])
    text = flow_mermaid(build_flow(a))
    assert 'S01["Say #quot;hi#quot; #91;now#93;"]' in text
    assert '"Say "hi"' not in text
    assert "    S01 --> S02" in text


# ------------------------------------------------------------------ svg


def test_svg_shape(flow: Flow) -> None:
    svg = flow_svg(flow)
    assert svg.startswith("<svg")
    assert 'viewBox="0 0 ' in svg
    assert svg.count("<rect") == len(flow.nodes)
    assert svg.count('class="edge"') == len(flow.edges)
    assert svg.count("<line") == len(flow.edges)  # the fixture is one forward hop after another
    assert "http" not in svg
    assert "<script" not in svg
    assert "Presses Save" in svg
    assert "5 fields · 1 action" in svg
    assert "2 fields · 2 actions" in svg


def test_svg_escapes_text_and_keeps_the_full_label_in_a_title() -> None:
    long_name = "Customer <details> & everything else about them"
    a = Analysis(title="t", summary="s", screens=[_screen("S01", long_name)])
    svg = flow_svg(build_flow(a))
    assert "<details>" not in svg
    assert "<title>S01 Customer &lt;details&gt; &amp; everything else about them</title>" in svg
    assert ">Customer &lt;details&gt; &amp; ever…</text>" in svg


def test_svg_single_row_up_to_six_then_snakes_in_rows_of_five() -> None:
    def ys(n: int) -> list[int]:
        a = Analysis(title="t", summary="s", screens=[_screen(f"S{i:02d}", f"Screen {i}") for i in range(1, n + 1)],
                     journey=[_step(i, f"S{i:02d}") for i in range(1, n + 1)])
        svg = flow_svg(build_flow(a))
        return [int(m) for m in re.findall(r'<rect x="\d+" y="(\d+)"', svg)]

    assert len(set(ys(6))) == 1
    seven = ys(7)
    assert len(seven) == 7
    assert seven[5] != seven[0] and seven[5] == seven[6]
    assert len(set(seven[:5])) == 1


def test_svg_width_and_height_match_the_layout(flow: Flow) -> None:
    svg = flow_svg(flow, node_width=100, node_height=40, gap_x=20, gap_y=10)
    match = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert match
    width, height = int(match.group(1)), int(match.group(2))
    assert width == 2 * MARGIN + 4 * 100 + 3 * 20
    assert height == TOP + MARGIN + 40
    assert f'style="max-width:{width}px;height:auto' in svg and 'width="100%"' in svg


def test_svg_arcs_backward_and_skipping_edges_over_the_boxes() -> None:
    a = Analysis(title="t", summary="s", screens=[_screen("S01", "A"), _screen("S02", "B"), _screen("S03", "C")],
                 actions=[_action("A001", "S01", "Skips ahead", "S03"), _action("A002", "S03", "Goes back", "S01")],
                 journey=[_step(1, "S01"), _step(2, "S02"), _step(3, "S03")])
    svg = flow_svg(build_flow(a))
    assert svg.count('<path class="edge"') == 2
    assert svg.count('<line class="edge"') == 2
    assert "Skips ahead" in svg and "Goes back" in svg


def test_svg_with_no_screens_still_renders() -> None:
    svg = flow_svg(Flow())
    assert svg.startswith("<svg") and "<rect" not in svg and "</svg>" in svg


# ------------------------------------------------------------------ markdown


def test_markdown_section(flow: Flow) -> None:
    text = flow_markdown(flow)
    assert text.startswith("## Screen flow\n")
    assert "```mermaid" in text
    assert "1. S01 Customer search → (Presses New customer) → S02 Customer details" in text
    assert "2. S02 Customer details → (Presses Save) → S03 Identity check" in text
    assert "3. S03 Identity check → (Presses Submit) → S04 Approvals queue" in text


def test_markdown_lists_journey_edges_without_a_label() -> None:
    a = Analysis(title="t", summary="s", screens=[_screen("S01", "A"), _screen("S02", "B")],
                 journey=[_step(1, "S01"), _step(2, "S02")])
    assert "1. S01 A → S02 B" in flow_markdown(build_flow(a))


def test_flow_model_round_trips() -> None:
    flow = Flow(nodes=[FlowNode(id="S01", label="A", order=1)], edges=[])
    assert Flow.model_validate_json(flow.model_dump_json()) == flow
