"""Parser for ConfigDumpInfo.xml (1C configuration dump manifest, format 2.13).

The manifest is the machine-readable tree of the whole configuration:

    <ConfigDumpInfo>
      <ConfigVersions>
        <Metadata name="Catalog.Номенклатура" id="<uuid>" configVersion="...">
          <Metadata name="Catalog.Номенклатура.Attribute.Цена" id="<uuid>"/>
          <Metadata name="Catalog.Номенклатура.TabularSection.Товары" id="...">
            <Metadata name="...TabularSection.Товары.Attribute.Сумма" id="..."/>
          </Metadata>
        </Metadata>
        ...
      </ConfigVersions>
    </ConfigDumpInfo>

This layer supplements the .txt report (ФАЗА 0): it provides the full tree,
English type prefixes (which name the dump directories), the stable object id
and the 1C-owned per-object configVersion checksum.

Path semantics (dotted name after `Type.ObjectName`):
    Attribute.Цена                                      -> element attribute
    TabularSection.Товары                               -> element tabular_section
    TabularSection.Товары.Attribute.Сумма               -> column of a tabular section
    Form.ФормаСписка                                    -> element form
    Form.ФормаСписка.Form                               -> (leaf) the form's own schema
    ObjectModule / ManagerModule / RecordSetModule      -> (leaf) modules
    Help / Rights                                       -> (leaf) non-element metadata

The segment sequence alternates container/name; the leaf's elem_type is its last
container segment, and its parent is the element named by the preceding name.

Design rules:
  * Tolerant: a broken/missing file yields an empty result, never an exception.
  * Source of truth for object names/synonyms is the .txt report; this module
    only carries english_type, source_key, id and config_version.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

from .config_parser import ConfigElement

log = logging.getLogger(__name__)

_NS = "{http://v8.1c.ru/8.3/xcf/dumpinfo}"

# English type prefix -> normalized type (matches the .txt report normalized keys)
EN_TYPE_MAP: dict[str, str] = {
    "Catalog": "catalog",
    "Document": "document",
    "AccumulationRegister": "accumulation_register",
    "InformationRegister": "information_register",
    "AccountingRegister": "accounting_register",
    "ChartOfCharacteristicTypes": "chart_of_characteristics",
    "ChartOfAccounts": "chart_of_accounts",
    "ExchangePlan": "exchange_plan",
    "Constant": "constant",
    "Enum": "enumeration",
    "Report": "report",
    "DataProcessor": "processing",
    "CommonModule": "common_module",
    "CommonForm": "common_form",
    "CommonTemplate": "common_template",
    "Role": "role",
    "Subsystem": "subsystem",
    "Task": "task",
    "BusinessProcess": "business_process",
    "DocumentJournal": "document_journal",
    "ScheduledJob": "scheduled_job",
    "HTTPService": "http_service",
    "WebService": "web_service",
    "WSReference": "ws_reference",
    "XDTOPackage": "xdto_package",
    "SettingsStorage": "settings_storage",
    "SessionParameter": "session_parameter",
    "EventSubscription": "event_subscription",
    "ExternalDataSource": "external_data_source",
    "Interface": "interface",
    "Style": "style",
    "StyleItem": "style_item",
    "Language": "language",
    "CommonPicture": "common_picture",
    "FilterCriterion": "filter_criterion",
}

# English type prefix -> dump directory name (where the object is unpacked)
EN_FOLDER_MAP: dict[str, str] = {
    "Catalog": "Catalogs",
    "Document": "Documents",
    "AccumulationRegister": "AccumulationRegisters",
    "InformationRegister": "InformationRegisters",
    "AccountingRegister": "AccountingRegisters",
    "ChartOfCharacteristicTypes": "ChartsOfCharacteristicTypes",
    "ChartOfAccounts": "ChartsOfAccounts",
    "ExchangePlan": "ExchangePlans",
    "Constant": "Constants",
    "Enum": "Enums",
    "Report": "Reports",
    "DataProcessor": "DataProcessors",
    "CommonModule": "CommonModules",
    "CommonForm": "CommonForms",
    "CommonTemplate": "CommonTemplates",
    "Role": "Roles",
    "Subsystem": "Subsystems",
    "Task": "Tasks",
    "BusinessProcess": "BusinessProcesses",
    "DocumentJournal": "DocumentJournals",
    "ScheduledJob": "ScheduledJobs",
    "HTTPService": "HTTPServices",
    "WebService": "WebServices",
    "WSReference": "WSReferences",
    "XDTOPackage": "XDTOPackages",
    "SettingsStorage": "SettingsStorages",
    "SessionParameter": "SessionParameters",
    "EventSubscription": "EventSubscriptions",
    "ExternalDataSource": "ExternalDataSources",
    "Interface": "Interfaces",
    "Style": "Styles",
    "StyleItem": "StyleItems",
    "Language": "Languages",
    "CommonPicture": "CommonPictures",
    "FilterCriterion": "FilterCriteria",
}

# Container segment -> elem_type (matches _ELEM_TYPE_MAP semantics of report_parser)
CONTAINER_ELEM_TYPE: dict[str, str] = {
    "Attribute": "attribute",
    "TabularSection": "tabular_section",
    "Form": "form",
    "Resource": "resource",
    "Dimension": "dimension",
    "Template": "template",
    "Command": "command",
    "Operation": "operation",
    "Parameter": "parameter",
    "EnumValue": "value",
    "Column": "column",
    "Predefined": "predefined_data",
    "Picture": "picture",
    "Table": "table",
    "Package": "package",
    "Schedule": "schedule",
    "Interface": "interface",
    "AddressingAttribute": "attribute",
    "URLTemplate": "operation",
    "Method": "parameter",
    "AccountingFlag": "attribute",
    "ExtDimensionAccountingFlag": "attribute",
}

# Leaf (non-element) segments: not turned into ConfigElement.
_LEAF_SEGMENTS = {
    "Form", "Help", "Rights",
    "ObjectModule", "ManagerModule", "RecordSetModule", "Module", "CommonModule",
}


def folder_for_type(english_type: str) -> str:
    """Dump directory name for an English type prefix, e.g. 'Catalog' -> 'Catalogs'."""
    return EN_FOLDER_MAP.get(english_type, english_type + "s")


# normalized type (as produced by the .txt report) -> English type prefix.
# Used to derive the source_key (Catalog.Колледжи) for a txt object.
NORM_TO_EN: dict[str, str] = {
    "catalog": "Catalog",
    "document": "Document",
    "accumulation_register": "AccumulationRegister",
    "information_register": "InformationRegister",
    "accounting_register": "AccountingRegister",
    "chart_of_characteristics": "ChartOfCharacteristicTypes",
    "chart_of_accounts": "ChartOfAccounts",
    "exchange_plan": "ExchangePlan",
    "constant": "Constant",
    "enumeration": "Enum",
    "report": "Report",
    "processing": "DataProcessor",
    "common_module": "CommonModule",
    "common_form": "CommonForm",
    "common_template": "CommonTemplate",
    "role": "Role",
    "subsystem": "Subsystem",
    "task": "Task",
    "business_process": "BusinessProcess",
    "document_journal": "DocumentJournal",
    "scheduled_job": "ScheduledJob",
}


def source_key_for(normalized_type: str, short_name: str) -> str:
    """Derive the source_key for a txt object, e.g. ('catalog','Колледжи') ->
    'Catalog.Колледжи'."""
    prefix = NORM_TO_EN.get(normalized_type, normalized_type)
    return f"{prefix}.{short_name}"


def parse_dumpinfo(path: str | Path) -> dict[str, dict]:
    """Parse ConfigDumpInfo.xml.

    Returns a mapping source_key -> object info:

        {
          "Catalog.Колледжи": {
              "id": "<uuid>",
              "config_version": "...",
              "english_type": "Catalog",
              "normalized_type": "catalog",
              "elements": [ConfigElement, ...],   # full nested tree
          },
          ...
        }

    Object names in `source_key` use the English prefix (Catalog.Колледжи); the
    .txt report provides the Russian-prefixed full name (Справочник.Колледжи).
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        log.warning("cannot read manifest %s: %s", path, e)
        return {}

    try:
        root = ET.fromstring(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ET.ParseError) as e:
        log.warning("manifest %s is not parseable: %s", path, e)
        return {}

    cv = root.find(f"{_NS}ConfigVersions")
    if cv is None:
        log.warning("manifest %s has no ConfigVersions", path)
        return {}

    result: dict[str, dict] = {}
    # per-object path-keyed element table: source_key -> {path_tuple: ConfigElement}
    tables: dict[str, dict[tuple[str, ...], ConfigElement]] = {}

    def _collect(el: ET.Element, source_key: str, table: dict) -> None:
        """Recursively collect nested Metadata children of one object node."""
        for child in el:
            if child.tag != f"{_NS}Metadata":
                continue
            child_parts = child.get("name", "").split(".")
            if len(child_parts) >= 3:
                _build_element(table, tuple(child_parts[2:]))
            _collect(child, source_key, table)

    for md in cv:
        if md.tag != f"{_NS}Metadata":
            continue
        parts = md.get("name", "").split(".")
        if len(parts) < 2 or parts[0] not in EN_TYPE_MAP:
            continue

        english_type = parts[0]
        source_key = f"{english_type}.{parts[1]}"
        info = result.setdefault(
            source_key,
            {
                "id": md.get("id", ""),
                "config_version": md.get("configVersion", ""),
                "english_type": english_type,
                "normalized_type": EN_TYPE_MAP[english_type],
                "elements": [],
            },
        )
        if not info["id"]:
            info["id"] = md.get("id", "")
        if not info["config_version"]:
            info["config_version"] = md.get("configVersion", "")
        table = tables.setdefault(source_key, {})
        _collect(md, source_key, table)

    # Link children into a nested tree per object.
    for source_key, table in tables.items():
        info = result[source_key]
        info["elements"] = _link_tree(table)

    return result


def _build_element(table: dict[tuple[str, ...], ConfigElement], path: tuple[str, ...]) -> None:
    """Register one element (and create missing ancestors) into the path table."""
    if path in table:
        return
    if not path or path[-1] in _LEAF_SEGMENTS:
        return
    # an element path must end with (container, name); a single segment is a leaf
    if len(path) < 2:
        return

    # Ensure ancestors exist (walk every (container, name) pair above us).
    parent_path = path[:-2] if len(path) >= 2 else ()
    if parent_path and parent_path not in table:
        _build_element(table, parent_path)

    elem_type = ""
    for seg in reversed(path[:-1]):
        if seg in CONTAINER_ELEM_TYPE:
            elem_type = CONTAINER_ELEM_TYPE[seg]
            break
    table[path] = ConfigElement(name=path[-1], elem_type=elem_type)


def _link_tree(table: dict[tuple[str, ...], ConfigElement]) -> list[ConfigElement]:
    """Build the nested element tree from the flat path table."""
    roots: list[ConfigElement] = []
    for path, elem in table.items():
        parent_path = path[:-2] if len(path) >= 2 else ()
        if parent_path and parent_path in table:
            table[parent_path].children.append(elem)
        else:
            roots.append(elem)
    for elem in table.values():
        elem.children.sort(key=lambda e: e.name)
    roots.sort(key=lambda e: e.name)
    return roots


def iter_object_xml(xml_root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (folder_name, object_xml_path) for per-object XML files, if present.

    Per-object XML files live at `<folder>/<Type>.<Name>.xml`; this helper scans
    the standard dump directory layout. Used by xml_object.py.
    """
    try:
        tops = sorted(xml_root.iterdir())
    except OSError:
        return
    for top in tops:
        if not top.is_dir():
            continue
        for f in sorted(top.glob("*.xml")):
            yield top.name, f
