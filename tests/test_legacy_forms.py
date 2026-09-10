"""Tests for the legacy (ordinary) form integration via the vendored parser.

Parses a synthetic Form.bin? No — the vendored parser needs real 1C binary
files. These tests cover the integration glue (bs_parser.parse_bs_text and the
xml_object.parse_legacy_object error paths) without real Form.bin fixtures.
"""

from __future__ import annotations

from pathlib import Path

from src.bs_parser import parse_bs_text
from src.xml_object import parse_legacy_object


def test_parse_bs_text_symbols():
    code = "Процедура ПриОткрытии()\n\tСообщить(\"x\");\nКонецПроцедуры\n"
    m = parse_bs_text(code, Path("Form.bin"), module_role="form:ФормаСписка",
                      object_name="Справочник.Абитуриенты")
    assert [s.name for s in m.symbols] == ["ПриОткрытии"]
    assert m.symbols[0].visibility == "local"
    assert m.symbols[0].object_name == "Справочник.Абитуриенты"


def test_parse_bs_text_empty():
    m = parse_bs_text("", Path("x"), module_role="form:x")
    assert m.symbols == []


def test_legacy_object_no_forms(tmp_path: Path):
    # directory without Form.bin -> None
    assert parse_legacy_object("Catalog.X", tmp_path) is None


def test_legacy_object_missing_dir(tmp_path: Path):
    assert parse_legacy_object("Catalog.X", tmp_path / "nope") is None
