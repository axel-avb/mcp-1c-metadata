"""Tests for the XML manifest/config/object layers (ФАЗА 1).

Runnable with pytest against a small synthetic dump fixture (no real AKADA data).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.xml_manifest import (
    EN_TYPE_MAP,
    folder_for_type,
    parse_dumpinfo,
    source_key_for,
)
from src.xml_config import global_is_legacy, parse_configuration
from src.xml_object import object_is_legacy


@pytest.fixture
def dump_dir(tmp_path: Path) -> Path:
    """A minimal synthetic 2.13 dump: manifest + config + one catalog with forms."""
    code = tmp_path / "code"
    code.mkdir(parents=True)

    manifest = code / "ConfigDumpInfo.xml"
    manifest.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ConfigDumpInfo xmlns="http://v8.1c.ru/8.3/xcf/dumpinfo" format="Hierarchical" version="2.13">
  <ConfigVersions>
    <Metadata name="Catalog.Номенклатура" id="11111111-1111-1111-1111-111111111111" configVersion="aaaa">
      <Metadata name="Catalog.Номенклатура.Attribute.Цена" id="22222222-2222-2222-2222-222222222222"/>
      <Metadata name="Catalog.Номенклатура.TabularSection.Товары" id="33333333-3333-3333-3333-333333333333">
        <Metadata name="Catalog.Номенклатура.TabularSection.Товары.Attribute.Сумма" id="44444444-4444-4444-4444-444444444444"/>
      </Metadata>
      <Metadata name="Catalog.Номенклатура.Form.ФормаЭлемента" id="55555555-5555-5555-5555-555555555555">
        <Metadata name="Catalog.Номенклатура.Form.ФормаЭлемента.Form"/>
      </Metadata>
    </Metadata>
    <Metadata name="Document.Реализация" id="66666666-6666-6666-6666-666666666666" configVersion="bbbb"/>
  </ConfigVersions>
</ConfigDumpInfo>
""",
        encoding="utf-8",
    )

    config = code / "Configuration.xml"
    config.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" version="2.13">
  <Configuration>
    <DefaultRunMode>OrdinaryApplication</DefaultRunMode>
    <InterfaceCompatibilityMode>Taxi</InterfaceCompatibilityMode>
  </Configuration>
</MetaDataObject>
""",
        encoding="utf-8",
    )

    # catalog with an ordinary (legacy) form and a managed form
    forms = code / "Catalogs" / "Номенклатура" / "Forms"
    forms.mkdir(parents=True)
    (forms / "ФормаЭлемента.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Form><Properties>'
        "<FormType>Ordinary</FormType></Properties></Form></MetaDataObject>",
        encoding="utf-8",
    )
    return code


def test_manifest_parse(dump_dir: Path):
    res = parse_dumpinfo(dump_dir / "ConfigDumpInfo.xml")
    assert "Catalog.Номенклатура" in res
    assert "Document.Реализация" in res

    info = res["Catalog.Номенклатура"]
    assert info["english_type"] == "Catalog"
    assert info["normalized_type"] == "catalog"
    assert info["config_version"] == "aaaa"
    assert info["id"].startswith("11111111")

    # element tree: attribute + tabular section + column
    names = {e.name: e.elem_type for e in info["elements"]}
    assert names["Цена"] == "attribute"
    ts = next(e for e in info["elements"] if e.elem_type == "tabular_section")
    assert ts.name == "Товары"
    assert [c.name for c in ts.children] == ["Сумма"]


def test_manifest_missing(tmp_path: Path):
    assert parse_dumpinfo(tmp_path / "nope.xml") == {}


def test_manifest_broken(tmp_path: Path):
    p = tmp_path / "broken.xml"
    p.write_text("<<<not xml", encoding="utf-8")
    assert parse_dumpinfo(p) == {}


def test_type_maps():
    assert EN_TYPE_MAP["Catalog"] == "catalog"
    assert folder_for_type("Catalog") == "Catalogs"
    assert folder_for_type("Enum") == "Enums"
    assert source_key_for("catalog", "Колледжи") == "Catalog.Колледжи"


def test_config_flags(dump_dir: Path):
    flags = parse_configuration(dump_dir)
    assert flags["run_mode"] == "ordinary"
    assert flags["interface_mode"] == "Taxi"
    assert global_is_legacy(flags) is True


def test_config_missing(tmp_path: Path):
    flags = parse_configuration(tmp_path)
    assert flags["run_mode"] == "managed"
    assert global_is_legacy(flags) is False


def test_object_is_legacy(dump_dir: Path):
    obj_dir = dump_dir / "Catalogs" / "Номенклатура"
    # has an Ordinary form -> always legacy
    assert object_is_legacy(obj_dir, global_legacy=False) is True
    # a formless object inherits the global flag
    empty = dump_dir / "Catalogs" / "Пустой"
    empty.mkdir(parents=True)
    assert object_is_legacy(empty, global_legacy=True) is True
    assert object_is_legacy(empty, global_legacy=False) is False
