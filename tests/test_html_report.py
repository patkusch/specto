"""The self-contained HTML report: one file, frames inside it, every item addressable by id."""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path

import pytest
from PIL import Image

from specto.html_report import esc, export_html, frame_alt
from specto.lint import format_findings, lint_analysis, summarize
from specto.model import Analysis, Recording

FIXTURES = Path(__file__).parent / "fixtures"
SIZE_CAP = 4 * 1024 * 1024


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


@pytest.fixture
def recording() -> Recording:
    return Recording.model_validate(json.loads((FIXTURES / "sample_recording.json").read_text()))


@pytest.fixture
def frames_on_disk(recording, tmp_path) -> list[Path]:
    """Six small JPEGs at the paths the fixture recording expects, each a different colour."""
    paths = []
    for i, kf in enumerate(recording.keyframes):
        path = tmp_path / kf.path
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), (40 * i, 120, 200 - 30 * i)).save(path, "JPEG")
        paths.append(path)
    return paths


def data_uris(text: str) -> list[str]:
    return re.findall(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", text)


# ------------------------------------------------------------------ without frames


def test_writes_file_with_ids_and_placeholders(analysis, recording, tmp_path):
    path = export_html(analysis, recording, tmp_path)
    assert path == tmp_path / "report.html"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>")
    assert f"<title>{analysis.title}</title>" in text
    for item in analysis.requirements + analysis.acceptance_criteria + analysis.questions + analysis.screens:
        assert f'id="{item.id}"' in text, item.id
    # No frame files in tmp_path, so every thumbnail is a grey placeholder and nothing is embedded.
    assert "data:image/jpeg;base64," not in text
    assert 'class="thumb placeholder"' in text
    assert "frame 0<br>" in text


def test_self_contained(analysis, recording, tmp_path):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    assert "<script src=" not in text
    assert "<link href=" not in text
    assert "<link rel=" not in text
    assert "@import" not in text
    assert "http://" not in text and "https://" not in text


def test_sections_in_order_with_toc(analysis, recording, tmp_path):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    ids = ["summary", "journey", "screens", "requirements", "questions", "transcript"]
    positions = [text.index(f'<section id="{i}">') for i in ids]
    assert positions == sorted(positions)
    toc = text[text.index('<nav class="toc">'):text.index("</nav>")]
    for i in ids:
        assert f'href="#{i}"' in toc
    assert text.index('<nav class="toc">') < positions[0]
    assert "@media print" in text


def test_summary_facts(analysis, recording, tmp_path):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    assert "<dt>Actors</dt><dd>Onboarding clerk, Team manager</dd>" in text
    assert f"<dt>Recording</dt><dd>{recording.source}</dd>" in text
    assert "<dt>Duration</dt><dd>05:00</dd>" in text
    assert f"<dt>Requirements</dt><dd>{len(analysis.requirements)}</dd>" in text
    assert f"<dt>Model</dt><dd>{analysis.usage.model}</dd>" in text
    assert f"<dt>Input tokens</dt><dd>{analysis.usage.input_tokens}</dd>" in text
    assert f"<dt>Writing check</dt><dd>{esc(summarize(lint_analysis(analysis)))}</dd>" in text


def test_requirement_card_content(analysis, recording, tmp_path):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    r = analysis.requirements[0]
    card = text[text.index(f'id="{r.id}"'):text.index('id="R002"')]
    assert r.statement in card
    assert r.rationale in card
    assert f"<blockquote>{r.source_quote}</blockquote>" in card
    for pill in (r.kind, r.priority, r.confidence):
        assert f'>{pill}</span>' in card
    assert 'id="AC001"' in card and 'id="AC002"' in card
    assert "Given" in card and "When" in card and "Then" in card
    # R002 joins two rules with "or"; its findings show in the muted check line, escaped.
    checks = lint_analysis(analysis)
    assert checks["R002"], "fixture R002 should have a finding"
    r2 = text[text.index('id="R002"'):text.index('id="R003"')]
    assert f'<p class="check">Writing check: {esc(format_findings(checks["R002"]))}</p>' in r2
    assert "&quot;or&quot;" in r2


def test_questions_have_editable_answer_cells(analysis, recording, tmp_path):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    cells = re.findall(r'<td class="answer" contenteditable="true"[^>]*></td>', text)
    assert len(cells) == len(analysis.questions)
    for q in analysis.questions:
        row = text[text.index(f'<tr id="{q.id}">'):]
        row = row[:row.index("</tr>")]
        assert q.question in row and q.why_it_matters in row and q.category in row
        assert 'contenteditable="true"' in row


def test_transcript_rows_and_filter_box(analysis, recording, tmp_path):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    section = text[text.index('<section id="transcript">'):text.index("</main>")]
    assert section.count('<tr><td class="when">') == len(recording.segments)
    assert 'data-filter="#transcript tbody tr"' in section
    assert "<td>Expert</td>" in section
    assert '<td class="when">00:02</td>' in section


def test_text_is_html_escaped(analysis, recording, tmp_path):
    analysis.requirements[0].statement = "Show <b>bold</b> & 'quotes' \"here\""
    analysis.questions[0].question = "Is <i>this</i> & that?"
    analysis.title = "Title <script>alert(1)</script>"
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    assert "<b>bold</b>" not in text
    assert "Show &lt;b&gt;bold&lt;/b&gt; &amp; &#x27;quotes&#x27; &quot;here&quot;" in text
    assert "Is &lt;i&gt;this&lt;/i&gt; &amp; that?" in text
    assert "<script>alert" not in text
    assert "<title>Title &lt;script&gt;alert(1)&lt;/script&gt;</title>" in text


def test_unknown_keyframe_gets_placeholder(analysis, recording, tmp_path, frames_on_disk):
    analysis.requirements[0].keyframe_index = 99
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    assert 'title="frame 99 at 00:15">frame 99<br>' in text


# ------------------------------------------------------------------ with frames


def test_frames_embedded_once_each_and_reused(analysis, recording, tmp_path, frames_on_disk):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    uris = data_uris(text)
    # One thumbnail and one large copy per keyframe, no more: reuse goes through <use>, not copies.
    assert len(uris) == 2 * len(recording.keyframes)
    assert 'class="thumb placeholder"' not in text
    for kf in recording.keyframes:
        assert f'<image id="t{kf.index}"' in text
        assert f'<use href="#t{kf.index}"/>' in text
        assert f'<figure class="big" id="big-{kf.index}">' in text
        assert f'href="#big-{kf.index}"' in text
    # Frame 0 is used by the journey, screens, requirements, questions and transcript.
    assert text.count('<use href="#t0"/>') >= 5
    # Each embedded image decodes as a JPEG of the promised size.
    widths = sorted({Image.open(io.BytesIO(base64.b64decode(u))).width for u in uris})
    assert widths == [480, 640]  # thumbnails at thumb_width; large copies at the source width (640 < 1280)


def test_thumb_width_and_quality_are_applied(analysis, recording, tmp_path, frames_on_disk):
    text = export_html(analysis, recording, tmp_path, filename="small.html", thumb_width=200).read_text(encoding="utf-8")
    widths = {Image.open(io.BytesIO(base64.b64decode(u))).width for u in data_uris(text)}
    assert 200 in widths
    assert 'viewBox="0 0 200 112"' in text or 'viewBox="0 0 200 113"' in text


def test_large_copy_is_capped_at_1280(analysis, recording, tmp_path):
    kf = recording.keyframes[0]
    path = tmp_path / kf.path
    path.parent.mkdir(parents=True)
    Image.new("RGB", (2560, 1440), (30, 30, 30)).save(path, "JPEG")
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    widths = sorted({Image.open(io.BytesIO(base64.b64decode(u))).width for u in data_uris(text)})
    assert widths == [480, 1280]


def test_thumbnail_alt_text(analysis, recording, tmp_path, frames_on_disk):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    assert frame_alt(7, 83) == "frame 7 at 01:23"
    r = analysis.requirements[0]
    assert f'aria-label="frame {r.keyframe_index} at 00:15"' in text
    assert f'<img alt="frame {r.keyframe_index} at 00:00"' in text  # the lightbox copy carries the keyframe's own time


def test_size_under_cap_with_frames(analysis, recording, tmp_path, frames_on_disk):
    path = export_html(analysis, recording, tmp_path)
    size = path.stat().st_size
    assert size < SIZE_CAP
    # Six flat frames should cost far less than the cap; a regression that duplicated images would show here.
    assert size < 400 * 1024


def test_no_javascript_needed_for_lightbox(analysis, recording, tmp_path, frames_on_disk):
    text = export_html(analysis, recording, tmp_path).read_text(encoding="utf-8")
    assert "figure.big:target { display:flex; }" in text
    assert text.count("<script>") == 1
