"""Unit tests for the standalone orchestrator's pure logic.

These cover the conversion-pipeline transforms that don't need the native
toolchain (pub2xhtml.exe / svg2pdf.exe) — so they run anywhere, incl. CI on
Linux, and lock in the two subtle correctness fixes (font-size unit rescale,
SVG page splitting) plus file discovery.
"""
import importlib.util
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parent.parent / "standalone" / "pub2pdf_app.py"
_spec = importlib.util.spec_from_file_location("pub2pdf_app", _APP)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


class TestFixFontSizes:
    def test_inches_to_points_scale(self):
        # libmspub emits font-size in inches; orchestrator rescales x72 to points.
        assert app.fix_font_sizes('<svg:text font-size="0.25"/>') == '<svg:text font-size="18.00"/>'

    def test_multiple_sizes_all_scaled(self):
        out = app.fix_font_sizes('a font-size="0.5" b font-size="1.0" c')
        assert 'font-size="36.00"' in out and 'font-size="72.00"' in out

    def test_no_font_size_unchanged(self):
        s = '<svg:rect width="100" height="50"/>'
        assert app.fix_font_sizes(s) == s

    def test_integer_font_size(self):
        assert app.fix_font_sizes('font-size="2"') == 'font-size="144.00"'


class TestExtractPages:
    def test_one_svg_one_page(self):
        xhtml = 'junk <svg:svg width="1">A</svg:svg> tail'
        pages = app.extract_pages(xhtml)
        assert len(pages) == 1
        assert pages[0].startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert "<svg:svg" in pages[0] and "</svg:svg>" in pages[0]

    def test_multiple_pages(self):
        xhtml = '<svg:svg>1</svg:svg>\n<svg:svg>2</svg:svg>\n<svg:svg>3</svg:svg>'
        assert len(app.extract_pages(xhtml)) == 3

    def test_no_svg_no_pages(self):
        assert app.extract_pages("<html><body>nothing</body></html>") == []

    def test_font_fix_applied_within_page(self):
        xhtml = '<svg:svg><svg:text font-size="0.5"/></svg:svg>'
        assert 'font-size="36.00"' in app.extract_pages(xhtml)[0]

    def test_svg_spanning_newlines(self):
        # DOTALL: a page's SVG can span many lines.
        xhtml = '<svg:svg>\n  line1\n  line2\n</svg:svg>'
        assert len(app.extract_pages(xhtml)) == 1


class TestCollectPubFiles:
    def test_single_file(self, tmp_path):
        f = tmp_path / "a.pub"; f.write_bytes(b"x")
        assert app.collect_pub_files(f, recurse=False) == [f]

    def test_rejects_non_pub(self, tmp_path):
        f = tmp_path / "a.txt"; f.write_bytes(b"x")
        with pytest.raises(SystemExit):
            app.collect_pub_files(f, recurse=False)

    def test_dir_nonrecursive_skips_subdirs(self, tmp_path):
        (tmp_path / "a.pub").write_bytes(b"x")
        (tmp_path / "b.pub").write_bytes(b"x")
        sub = tmp_path / "sub"; sub.mkdir(); (sub / "c.pub").write_bytes(b"x")
        found = app.collect_pub_files(tmp_path, recurse=False)
        assert [p.name for p in found] == ["a.pub", "b.pub"]

    def test_dir_recursive_includes_subdirs(self, tmp_path):
        (tmp_path / "a.pub").write_bytes(b"x")
        sub = tmp_path / "sub"; sub.mkdir(); (sub / "c.pub").write_bytes(b"x")
        found = app.collect_pub_files(tmp_path, recurse=True)
        assert {p.name for p in found} == {"a.pub", "c.pub"}

    def test_case_insensitive_extension(self, tmp_path):
        f = tmp_path / "A.PUB"; f.write_bytes(b"x")
        assert app.collect_pub_files(f, recurse=False) == [f]

    def test_missing_path_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            app.collect_pub_files(tmp_path / "nope", recurse=False)


class TestGatherFromInputs:
    def test_dedups_file_and_its_folder(self, tmp_path):
        a = tmp_path / "a.pub"; a.write_bytes(b"x")
        b = tmp_path / "b.pub"; b.write_bytes(b"x")
        # folder + an explicit file already inside it → no duplicate
        out = app.gather_from_inputs([str(tmp_path), str(a)], recurse=False)
        assert sorted(p.name for p in out) == ["a.pub", "b.pub"]

    def test_mixes_files_and_folders(self, tmp_path):
        d1 = tmp_path / "d1"; d1.mkdir(); (d1 / "x.pub").write_bytes(b"x")
        loose = tmp_path / "loose.pub"; loose.write_bytes(b"x")
        out = app.gather_from_inputs([str(d1), str(loose)], recurse=False)
        assert {p.name for p in out} == {"x.pub", "loose.pub"}

    def test_recurse_flag_respected(self, tmp_path):
        sub = tmp_path / "sub"; sub.mkdir(); (sub / "deep.pub").write_bytes(b"x")
        assert app.gather_from_inputs([str(tmp_path)], recurse=False) == []
        assert len(app.gather_from_inputs([str(tmp_path)], recurse=True)) == 1


class TestRunBatch:
    def _stub(self, monkeypatch, fail_names=()):
        calls = []
        def fake_convert(pub_file, pdf_path, tools):
            calls.append(pub_file.name)
            if pub_file.name in fail_names:
                raise RuntimeError("boom")
            pdf_path.parent.mkdir(parents=True, exist_ok=True)  # real convert_one does this
            pdf_path.write_bytes(b"%PDF-1.4 fake")
            return 3
        monkeypatch.setattr(app, "convert_one", fake_convert)
        return calls

    def test_counts_and_progress(self, tmp_path, monkeypatch):
        self._stub(monkeypatch)
        files = [tmp_path / f"f{i}.pub" for i in range(3)]
        for f in files:
            f.write_bytes(b"x")
        events = []
        r = app.run_batch(files, tmp_path / "out", force=False, tools=Path("bin"),
                          progress=lambda *a: events.append(a))
        assert (r["converted"], r["skipped"], r["failed"]) == (3, 0, 0)
        assert len(events) == 3 and events[0][1] == 3  # total reported

    def test_skip_existing_unless_force(self, tmp_path, monkeypatch):
        self._stub(monkeypatch)
        f = tmp_path / "a.pub"; f.write_bytes(b"x")
        (tmp_path / "a.pdf").write_bytes(b"old")
        assert app.run_batch([f], None, force=False, tools=Path("b"))["skipped"] == 1
        assert app.run_batch([f], None, force=True, tools=Path("b"))["converted"] == 1

    def test_one_failure_does_not_abort(self, tmp_path, monkeypatch):
        self._stub(monkeypatch, fail_names={"bad.pub"})
        files = [tmp_path / "ok1.pub", tmp_path / "bad.pub", tmp_path / "ok2.pub"]
        for f in files:
            f.write_bytes(b"x")
        r = app.run_batch(files, tmp_path / "o", force=True, tools=Path("b"))
        assert (r["converted"], r["failed"]) == (2, 1)
        assert r["failures"] == [("bad.pub", "boom")]
