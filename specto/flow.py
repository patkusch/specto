"""Stage 3, a picture of the journey: screens as boxes, actions as arrows.

`build_flow` turns an Analysis into a small graph. `flow_mermaid` writes it as
a Mermaid block that GitHub renders inside report.md, `flow_svg` draws it as an
inline SVG for report.html (no scripts, no external resources, so it prints and
opens offline), and `flow_markdown` wraps the Mermaid block in a section with a
plain numbered list for viewers that do not render diagrams.
"""
from __future__ import annotations

import html
import re
from typing import Literal

from pydantic import BaseModel, Field

from specto.model import Analysis

MAX_LABEL = 26
NODES_PER_ROW = 5
SINGLE_ROW_UP_TO = 6
MARGIN = 12
TOP = 40  # room above the first row for arrows that arc over the boxes, and their labels


class FlowNode(BaseModel):
    id: str
    label: str
    order: int = Field(description="1 for the first screen in the journey; screens never visited come after the last visited one")
    n_fields: int = 0
    n_actions: int = 0


class FlowEdge(BaseModel):
    from_id: str
    to_id: str
    label: str = ""
    kind: Literal["action", "journey"]


class Flow(BaseModel):
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[FlowEdge] = Field(default_factory=list)


# ------------------------------------------------------------------ the graph


def build_flow(analysis: Analysis) -> Flow:
    """Nodes are screens in the order the expert reached them; edges are actions that lead somewhere, plus journey steps."""
    known = {s.id for s in analysis.screens}
    order: dict[str, int] = {}
    for step in sorted(analysis.journey, key=lambda j: j.order):
        if step.screen_id in known and step.screen_id not in order:
            order[step.screen_id] = len(order) + 1
    for s in analysis.screens:
        order.setdefault(s.id, len(order) + 1)

    n_fields = {s.id: sum(1 for f in analysis.fields if f.screen_id == s.id) for s in analysis.screens}
    n_actions = {s.id: sum(1 for a in analysis.actions if a.screen_id == s.id) for s in analysis.screens}
    nodes = sorted(
        (FlowNode(id=s.id, label=s.name, order=order[s.id], n_fields=n_fields[s.id], n_actions=n_actions[s.id])
         for s in analysis.screens),
        key=lambda n: n.order,
    )

    edges: list[FlowEdge] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(from_id: str, to_id: str, label: str, kind: str) -> None:
        if from_id == to_id or from_id not in known or to_id not in known:
            return
        key = (from_id, to_id, label, kind)
        if key in seen:
            return
        seen.add(key)
        edges.append(FlowEdge(from_id=from_id, to_id=to_id, label=label, kind=kind))

    for a in analysis.actions:
        if a.leads_to_screen_id:
            add(a.screen_id, a.leads_to_screen_id, a.description.strip(), "action")
    joined = {(e.from_id, e.to_id) for e in edges}
    steps = sorted(analysis.journey, key=lambda j: j.order)
    for prev, nxt in zip(steps, steps[1:]):
        if prev.screen_id != nxt.screen_id and (prev.screen_id, nxt.screen_id) not in joined:
            add(prev.screen_id, nxt.screen_id, "", "journey")

    edges.sort(key=lambda e: (order.get(e.from_id, 0), e.kind != "action", order.get(e.to_id, 0)))
    return Flow(nodes=nodes, edges=edges)


# ------------------------------------------------------------------ mermaid


def _mermaid_id(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", node_id) or "_"


def _mermaid_label(text: str) -> str:
    """Mermaid entity codes for the characters that break a quoted label."""
    return (text.replace("#", "#35;").replace('"', "#quot;").replace("[", "#91;").replace("]", "#93;")
            .replace("<", "#lt;").replace(">", "#gt;"))


def flow_mermaid(flow: Flow) -> str:
    """A `flowchart LR` block in a mermaid fence; GitHub draws it in report.md."""
    lines = ["```mermaid", "flowchart LR"]
    for n in flow.nodes:
        lines.append(f'    {_mermaid_id(n.id)}["{_mermaid_label(n.label)}"]')
    for e in flow.edges:
        a, b = _mermaid_id(e.from_id), _mermaid_id(e.to_id)
        if e.kind == "action" and e.label:
            lines.append(f'    {a} -- "{_mermaid_label(e.label)}" --> {b}')
        else:
            lines.append(f"    {a} --> {b}")
    lines.append("```")
    return "\n".join(lines)


# ------------------------------------------------------------------ svg


def _short(text: str, limit: int = MAX_LABEL) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _positions(n: int, w: int, h: int, gap_x: int, gap_y: int) -> tuple[list[tuple[int, int]], int, int]:
    """Top-left corner of each box, plus the total width and height. Six or fewer in one row; else rows of five, snaking."""
    per_row = n if n <= SINGLE_ROW_UP_TO else NODES_PER_ROW
    cols = min(n, per_row) if n else 0
    rows = (n + per_row - 1) // per_row if n else 0
    boxes = []
    for i in range(n):
        r, c = divmod(i, per_row)
        if r % 2 == 1:
            c = per_row - 1 - c
        boxes.append((MARGIN + c * (w + gap_x), TOP + r * (h + gap_y)))
    width = 2 * MARGIN + cols * w + max(cols - 1, 0) * gap_x
    height = TOP + MARGIN + rows * h + max(rows - 1, 0) * gap_y
    return boxes, width, height


def _edge_shape(a: tuple[int, int], b: tuple[int, int], w: int, h: int, gap_x: int, lift: int) -> tuple[str, int, int]:
    """The line or path for one arrow, and where its label goes.

    A forward hop to the next box on the same row is a straight line. Any other
    move on the same row (backwards, or skipping a box) arcs over the top so it
    is not hidden behind the boxes in between. A move to another row goes
    straight down or up from the middle of the box.
    """
    (ax, ay), (bx, by) = a, b
    stroke = 'stroke="#6a737d" stroke-width="1.2" fill="none" marker-end="url(#flow-arrow)"'
    if ay == by:
        if bx - ax == w + gap_x:
            y = ay + h // 2
            return f'<line class="edge" x1="{ax + w}" y1="{y}" x2="{bx}" y2="{y}" {stroke}/>', (ax + w + bx) // 2, y - 4
        top = ay - lift
        d = f"M {ax + w // 2} {ay} L {ax + w // 2} {top} L {bx + w // 2} {top} L {bx + w // 2} {by}"
        return f'<path class="edge" d="{d}" {stroke}/>', (ax + bx + w) // 2, top - 3
    if by > ay:
        x1, y1, x2, y2 = ax + w // 2, ay + h, bx + w // 2, by
    else:
        x1, y1, x2, y2 = ax + w // 2, ay, bx + w // 2, by + h
    return f'<line class="edge" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" {stroke}/>', (x1 + x2) // 2 + 4, (y1 + y2) // 2


def flow_svg(flow: Flow, node_width: int = 180, node_height: int = 56, gap_x: int = 60, gap_y: int = 24) -> str:
    """An inline SVG of the flow. Plain grey boxes and dark text, no scripts or external resources.

    It has a viewBox and a percentage width capped at its natural size, so it
    shrinks to fit a narrow page or a printed sheet and never blows up.
    """
    w, h = node_width, node_height
    boxes, width, height = _positions(len(flow.nodes), w, h, gap_x, gap_y)
    at = {n.id: box for n, box in zip(flow.nodes, boxes)}
    if not flow.nodes:
        width, height = 2 * MARGIN + 160, TOP + MARGIN
    out = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px;height:auto;display:block" '
        f'role="img" aria-label="Screen flow" font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif">',
        '<defs><marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#6a737d"/></marker></defs>',
    ]
    if not flow.nodes:
        out.append(f'<text x="{MARGIN}" y="{TOP}" font-size="12" fill="#6a737d">No screens recorded.</text>')
    labels: list[str] = []
    arcs = 0  # arcs alternate between two heights so two over the same boxes stay apart
    for e in flow.edges:
        shape, lx, ly = _edge_shape(at[e.from_id], at[e.to_id], w, h, gap_x, lift=10 + 12 * (arcs % 2))
        arcs += shape.startswith("<path")
        out.append(shape)
        if e.kind == "action" and e.label:
            labels.append(f'<text x="{lx}" y="{ly}" font-size="10" fill="#1f2328" text-anchor="middle" '
                          f'stroke="#ffffff" stroke-width="3" paint-order="stroke">'
                          f'<title>{html.escape(e.label)}</title>{html.escape(_short(e.label, 24))}</text>')
    for n in flow.nodes:
        x, y = at[n.id]
        sub = f"{_count(n.n_fields, 'field')} · {_count(n.n_actions, 'action')}"
        out.append(
            f'<g><title>{html.escape(n.id)} {html.escape(n.label)}</title>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="#ffffff" stroke="#d8dde3" stroke-width="1"/>'
            f'<text x="{x + w // 2}" y="{y + 22}" font-size="13" font-weight="600" fill="#1f2328" text-anchor="middle">{html.escape(_short(n.label))}</text>'
            f'<text x="{x + w // 2}" y="{y + 40}" font-size="10" fill="#6a737d" text-anchor="middle">{html.escape(sub)}</text></g>'
        )
    out += labels  # last, so they sit on top of the boxes they overlap
    out.append("</svg>")
    return "\n".join(out)


# ------------------------------------------------------------------ markdown


def flow_list(flow: Flow) -> list[str]:
    """One line per edge, e.g. 'S01 Customer search → (Presses Save) → S02 Customer details'."""
    names = {n.id: n.label for n in flow.nodes}
    lines = []
    for e in flow.edges:
        via = f" → ({e.label})" if e.kind == "action" and e.label else ""
        lines.append(f"{e.from_id} {names.get(e.from_id, e.from_id)}{via} → {e.to_id} {names.get(e.to_id, e.to_id)}")
    return lines


def flow_markdown(flow: Flow) -> str:
    """The 'Screen flow' section of report.md: how to read it, the diagram, and a plain list as a fallback."""
    lines = [
        "## Screen flow", "",
        "Each box is a screen the expert showed, in the order they reached them; a labelled arrow is the action "
        "that moves from one screen to the next, and an unlabelled arrow is a step of the journey with no recorded action between the two.",
        "", flow_mermaid(flow), "",
    ]
    steps = flow_list(flow)
    if steps:
        lines += ["If the diagram above does not show, read the same flow as a list:", ""]
        lines += [f"{i}. {line}" for i, line in enumerate(steps, start=1)]
    else:
        lines.append("No moves between screens were recorded.")
    lines.append("")
    return "\n".join(lines)
