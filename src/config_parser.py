"""Parser for 1C configuration source XML export.

.. deprecated::
    This XML-based parser is **deprecated** and will be replaced by a new
    parser in a future revision. Do not extend this module.

Expected layout (standard "configuration in source code" export):

    <root>/
      Config.xml                     # root metadata + object list
      Catalogs/Catalog.X/Info.xml    # one folder per object
      Documents/Document.X/Info.xml
      Registers/AccumulationRegisters/Register.X/Info.xml
      ...

The parser is tolerant: it accepts both flat element forms (<Type>Catalog</Type>)
and property-pair forms (<Property><Name>Type</Name><Value>Catalog</Value></Property>).

If your export uses a different schema, adjust the normalization in `_props()`
or provide a sample and we'll adapt.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import warnings

warnings.warn(
    "config_parser is deprecated and will be replaced by a new parser. "
    "Do not extend this module.",
    DeprecationWarning,
    stacklevel=2,
)

# English type (as in Config.xml) -> (normalized key, folder names to scan)
TYPE_MAP: dict[str, str] = {
    "Catalog": "catalog",
    "Document": "document",
    "AccumulationRegister": "accumulation_register",
    "InformationRegister": "information_register",
    "JournalRegister": "journal_register",
    "CalculationRegister": "calculation_register",
    "Constant": "constant",
    "Enumeration": "enumeration",
    "ChartOfCharacteristics": "chart_of_characteristics",
    "ExchangePlan": "exchange_plan",
    "ChartOfAccounts": "chart_of_accounts",
    "Subsystem": "subsystem",
    "Library": "library",
}

# Russian type names -> normalized key (for exports where Type is in Russian)
_RU_TYPE_MAP: dict[str, str] = {
    "Каталог": "catalog",
    "Документ": "document",
    "РегистрНакопления": "accumulation_register",
    "РегистрСведений": "information_register",
    "РегистрБухгалтерии": "journal_register",
    "РегистрРасчётов": "calculation_register",
    "Константа": "constant",
    "Перечисление": "enumeration",
    "ПланВидовХарактеристик": "chart_of_characteristics",
    "ПланОбмена": "exchange_plan",
    "ПланСчетов": "chart_of_accounts",
    "Подсистема": "subsystem",
    "Библиотека": "library",
}

# normalized type -> Russian 1C type name (for full object names "Каталог.Номенклатура")
RU_TYPE_BY_KEY: dict[str, str] = {
    "catalog": "Каталог",
    "document": "Документ",
    "accumulation_register": "РегистрНакопления",
    "information_register": "РегистрСведений",
    "journal_register": "РегистрБухгалтерии",
    "calculation_register": "РегистрРасчётов",
    "constant": "Константа",
    "enumeration": "Перечисление",
    "chart_of_characteristics": "ПланВидовХарактеристик",
    "exchange_plan": "ПланОбмена",
    "chart_of_accounts": "ПланСчетов",
    "subsystem": "Подсистема",
    "library": "Библиотека",
}

# folder name -> normalized type (top-level folders in the export)
FOLDER_TYPE_MAP: dict[str, str] = {
    "Catalogs": "catalog",
    "Documents": "document",
    "Constants": "constant",
    "Enumerations": "enumeration",
    "ChartsOfCharacteristics": "chart_of_characteristics",
    "ExchangePlans": "exchange_plan",
    "AccountCharts": "chart_of_accounts",
    "Subsystems": "subsystem",
    "Libraries": "library",
}

REGISTER_SUBFOLDERS: dict[str, str] = {
    "AccumulationRegisters": "accumulation_register",
    "InformationRegisters": "information_register",
    "JournalRegisters": "journal_register",
    "CalculationRegisters": "calculation_register",
}

# Russian type prefixes that can appear in DataItemType / Reference(...)
_RU_REF_PREFIXES = (
    "Каталог|Документ|Константа|Перечисление|РегистрНакопления|РегистрСведений|"
    "РегистрБухгалтерии|РегистрРасчётов"
)
_REF_OBJ_RE = re.compile(
    r"Reference\(\s*'?([^'()\s]+\.[^'()\s]+)'?\s*\)"
    rf"|^({_RU_REF_PREFIXES})\.(\S+)$"
)


@dataclass
class ConfigElement:
    """An element of a configuration object: attribute, tabular section, command..."""

    name: str
    elem_type: str = ""          # Attribute / TabularSection / Command / ...
    synonym: str = ""
    comment: str = ""
    data_type: str = ""          # String(20), Ref, ChoiceRef, Decimal(10,2)...
    ref_object: str = ""         # e.g. "Каталог.Номенклатура" if this is a reference
    children: list["ConfigElement"] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def display(self) -> str:
        parts = [self.name]
        if self.synonym and self.synonym != self.name:
            parts.append(f"({self.synonym})")
        return " ".join(parts)


@dataclass
class ConfigObject:
    """A configuration object (catalog, document, register, ...)."""

    name: str                    # e.g. "Каталог.Номенклатура"
    type: str                    # normalized: catalog, document, ...
    synonym: str = ""
    comment: str = ""
    folder: Path | None = None
    elements: list[ConfigElement] = field(default_factory=list)
    module_files: dict[str, Path] = field(default_factory=dict)  # role -> path

    @property
    def node_id(self) -> str:
        return f"{self.type}:{self.name}"

    @property
    def short_name(self) -> str:
        return self.name.split(".", 1)[-1] if "." in self.name else self.name


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _props_from_item(item: ET.Element) -> dict[str, str]:
    """Extract properties from <Property><Name>x</Name><Value>y</Value></Property>
    and/or direct child elements like <Type>Catalog</Type>."""
    props: dict[str, str] = {}
    for child in item:
        tag = _strip_ns(child.tag)
        if tag == "Property":
            name = _text(child.find("Name"))
            value = _text(child.find("Value"))
            if name:
                props[name] = value
        elif tag not in ("Items", "Name", "Synonym", "Comment", "Properties"):
            props[tag] = _text(child)
    return props


def _ref_object_from(props: dict[str, str]) -> str:
    for key in ("RefObject", "ReferenceObject", "Ref"):
        val = props.get(key, "")
        if val and "." in val:
            return val
    dt = props.get("DataItemType", "")
    m = _REF_OBJ_RE.search(dt)
    if not m:
        return ""
    if m.lastindex == 1:
        raw = m.group(1)
        # "КаталогСсылка.Номенклатура" -> "Каталог.Номенклатура"
        if "." in raw:
            first, rest = raw.split(".", 1)
            base = first.removesuffix("Ссылка")
            if base in RU_TYPE_BY_KEY.values():
                return f"{base}.{rest}"
        return raw
    return f"{m.group(2)}.{m.group(3)}"


def _parse_item(item: ET.Element) -> ConfigElement:
    name = _text(item.find("Name")) or "(unnamed)"
    props = _props_from_item(item)
    elem = ConfigElement(
        name=name,
        elem_type=props.get("Type", ""),
        synonym=_text(item.find("Synonym")) or props.get("Synonym", ""),
        comment=_text(item.find("Comment")) or props.get("Comment", ""),
        data_type=props.get("DataItemType", ""),
        ref_object=_ref_object_from(props),
        properties=props,
    )
    for sub in item.findall("Items/Item"):
        elem.children.append(_parse_item(sub))
    return elem


def _normalize_type(raw: str) -> str:
    raw = raw.strip()
    if raw in TYPE_MAP:
        return TYPE_MAP[raw]
    if raw in _RU_TYPE_MAP:
        return _RU_TYPE_MAP[raw]
    return raw.lower()


def parse_info_xml(path: Path, default_type: str | None = None) -> ConfigObject | None:
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return None
    root = tree.getroot()

    props = _props_from_item(root)
    name = _text(root.find("Name")) or props.get("Name", "")
    if not name:
        # fall back to folder name, e.g. "Catalog.Nomenclature"
        name = path.parent.name.replace("-", ".")
    obj_type = _normalize_type(props.get("Type", "")) if props.get("Type") else (default_type or "")

    # Info.xml usually holds the SHORT name ("Номенклатура"); the full 1C name is
    # "Каталог.Номенклатура". Prefix it unless the export already gave a full name.
    if "." not in name and obj_type in RU_TYPE_BY_KEY:
        prefix = RU_TYPE_BY_KEY[obj_type]
        if not name.startswith(prefix + "."):
            name = f"{prefix}.{name}"

    obj = ConfigObject(
        name=name,
        type=obj_type,
        synonym=_text(root.find("Synonym")) or props.get("Synonym", ""),
        comment=_text(root.find("Comment")) or props.get("Comment", ""),
        folder=path.parent,
    )
    for item in root.findall("Items/Item"):
        obj.elements.append(_parse_item(item))

    # module files: ObjectModule.bsl, ManagerModule.bsl, FormModule.bsl (per form)
    if obj.folder:
        for f in sorted(obj.folder.iterdir()):
            if f.suffix.lower() in (".bsl", ".bs"):
                role = f.stem.replace("Module", "").replace("-", "")
                obj.module_files[role or "module"] = f
    return obj


def _iter_object_dirs(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (normalized_type, object_folder) for every object folder."""
    for top in sorted(root.iterdir()):
        if not top.is_dir():
            continue
        if top.name in FOLDER_TYPE_MAP:
            t = FOLDER_TYPE_MAP[top.name]
            for obj_dir in sorted(top.iterdir()):
                if obj_dir.is_dir() and (obj_dir / "Info.xml").is_file():
                    yield t, obj_dir
        elif top.name == "Registers":
            for sub in sorted(top.iterdir()):
                if not sub.is_dir() or sub.name not in REGISTER_SUBFOLDERS:
                    continue
                t = REGISTER_SUBFOLDERS[sub.name]
                for obj_dir in sorted(sub.iterdir()):
                    if obj_dir.is_dir() and (obj_dir / "Info.xml").is_file():
                        yield t, obj_dir


def parse_config_root(root: Path) -> list[ConfigObject]:
    """Parse the whole configuration export. Returns objects (order stable)."""
    objects: list[ConfigObject] = []
    for obj_type, obj_dir in _iter_object_dirs(root):
        info = obj_dir / "Info.xml"
        obj = parse_info_xml(info, default_type=obj_type)
        if obj is not None:
            objects.append(obj)
    return objects


def object_text_repr(obj: ConfigObject, element: ConfigElement | None = None) -> str:
    """Build the text representation of an object/element for embedding."""
    lines = [f"{obj.type} {obj.name}"]
    if obj.synonym and obj.synonym != obj.name:
        lines.append(f"Синоним: {obj.synonym}")
    if obj.comment:
        lines.append(f"Предназначение: {obj.comment}")

    def add_elem(e: ConfigElement, indent: str = "") -> None:
        header = f"{indent}{e.elem_type or 'Элемент'}: {e.display}"
        if e.data_type:
            header += f" [{e.data_type}]"
        if e.ref_object:
            header += f" -> {e.ref_object}"
        lines.append(header)
        if e.comment:
            lines.append(f"{indent}  Предназначение: {e.comment}")
        for c in e.children:
            add_elem(c, indent + "  ")

    target = [element] if element is not None else obj.elements
    for e in target:
        add_elem(e)
    return "\n".join(lines)


def element_text_repr(obj: ConfigObject, element: ConfigElement) -> str:
    return object_text_repr(obj, element)


def stable_id(*parts: str) -> str:
    """Stable id for a node from its logical parts.

    Returns a dashed UUID string (36 chars) — accepted by Qdrant as a point id
    and fine as a SQLite primary key.
    """
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
