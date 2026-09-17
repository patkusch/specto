"""Wiki export tests: the Confluence and SharePoint pages parse as well-formed
XHTML, cover every requirement and question, and carry no script."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from specto.export import export_all
from specto.model import Analysis, Recording
from specto.wiki_export import export_confluence_page, export_sharepoint_page, export_wiki_pages

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


@pytest.fixture
def recording() -> Recording:
    return Recording.model_validate(json.loads((FIXTURES / "sample_recording.json").read_text()))


class TestConfluence:
    @pytest.fixture
    def page(self, analysis, recording, tmp_path) -> Path:
        path = export_confluence_page(analysis, recording, tmp_path)
        assert path == tmp_path / "confluence.html"
        return path

    def test_well_formed_xml(self, page):
        ET.parse(page)  # raises xml.etree.ElementTree.ParseError if it is not well-formed

    def test_no_script_tag(self, page):
        assert "<script" not in page.read_text(encoding="utf-8").lower()

    def test_no_report_html_interactivity(self, page):
        # This is a static import target, not a copy of report.html.
        text = page.read_text(encoding="utf-8").lower()
        assert "localstorage" not in text
        assert "contenteditable" not in text

    def test_covers_every_requirement_and_question(self, page, analysis):
        text = page.read_text(encoding="utf-8")
        for r in analysis.requirements:
            assert r.id in text
            assert r.statement in text
        for ac in analysis.acceptance_criteria:
            assert ac.id in text
        for q in analysis.questions:
            assert q.id in text
            assert q.question in text
        for s in analysis.screens:
            assert s.id in text and s.name in text

    def test_title_and_summary(self, page, analysis):
        text = page.read_text(encoding="utf-8")
        assert f"<title>{analysis.title}</title>" in text
        assert analysis.summary in text


class TestSharePoint:
    @pytest.fixture
    def page(self, analysis, recording, tmp_path) -> Path:
        path = export_sharepoint_page(analysis, recording, tmp_path)
        assert path == tmp_path / "sharepoint.html"
        return path

    def test_well_formed_xml(self, page):
        ET.parse(page)

    def test_no_script_tag(self, page):
        assert "<script" not in page.read_text(encoding="utf-8").lower()

    def test_covers_every_requirement_and_question(self, page, analysis):
        text = page.read_text(encoding="utf-8")
        for r in analysis.requirements:
            assert r.id in text
        for q in analysis.questions:
            assert q.id in text


def test_confluence_and_sharepoint_differ_only_in_the_wrapper(analysis, recording, tmp_path):
    confluence = export_confluence_page(analysis, recording, tmp_path).read_text(encoding="utf-8")
    sharepoint = export_sharepoint_page(analysis, recording, tmp_path).read_text(encoding="utf-8")
    # Same tables, different shell: the body content is identical, only the head/meta differ.
    assert confluence.split("<body>\n", 1)[1] == sharepoint.split("<body>\n", 1)[1]
    assert "Confluence" in confluence and "SharePoint" not in confluence
    assert "SharePoint" in sharepoint and "Confluence" not in sharepoint


def test_export_wiki_pages_writes_both(analysis, recording, tmp_path):
    paths = export_wiki_pages(analysis, recording, tmp_path)
    assert set(paths) == {"confluence_html", "sharepoint_html"}
    assert paths["confluence_html"] == tmp_path / "confluence.html"
    assert paths["sharepoint_html"] == tmp_path / "sharepoint.html"
    assert all(p.exists() and p.stat().st_size > 0 for p in paths.values())


def test_export_wiki_pages_creates_out_dir(analysis, recording, tmp_path):
    out = tmp_path / "nested" / "out"
    paths = export_wiki_pages(analysis, recording, out)
    assert all(p.parent == out and p.exists() for p in paths.values())


def test_export_all_includes_wiki_pages(analysis, recording, tmp_path):
    paths = export_all(analysis, recording, tmp_path)
    assert {"confluence_html", "sharepoint_html"} <= set(paths)
    assert paths["confluence_html"] == tmp_path / "confluence.html"
    assert paths["sharepoint_html"] == tmp_path / "sharepoint.html"
    assert all(p.exists() for p in paths.values())


def test_empty_analysis_still_writes_valid_pages(recording, tmp_path):
    empty = Analysis(title="Nothing", summary="Nothing was found.")
    paths = export_wiki_pages(empty, recording, tmp_path)
    for path in paths.values():
        ET.parse(path)
