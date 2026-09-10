"""
Mapping tables for 1C Form.bin curly-brace format → managed form XML equivalents.

Maps UUIDs, type IDs, and property patterns from the binary ordinary form format
to their managed form XML tag names, event names, and property names.
"""
from __future__ import annotations

from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Control UUID → XML tag name (managed form equivalent)
# ---------------------------------------------------------------------------

CONTROL_UUID_TO_XML_TAG: Dict[str, str] = {
    # Groups / containers
    "e69bf21d-97b2-4f37-86db-675aea9ec2cb": "UsualGroup",
    "90db814a-c75f-4b54-bc96-df62e554d67d": "UsualGroup",
    # Input controls (plain text/number/date entry)
    "381ed624-9217-4e63-85db-c4c3cb87daae": "InputField",
    "64483e7f-3833-48e2-8c75-2c31aac49f6e": "InputField",  # with ChoiceList
    # Table fields (value table / list)
    "ea83fe3a-ac3c-4cce-8045-3dddf35b28b1": "Table",
    "236a17b3-7f44-46d9-a907-75f9cdc61ab5": "SpreadSheetDocumentField",
    # Radio buttons (переключатель)
    "782e569a-79a7-4a4f-a936-b48d013936ec": "RadioButtonField",
    # Labels / decorations
    "0fc7e20d-f241-460c-bdf4-5ad88e5474a5": "LabelField",
    # Buttons
    "6ff79819-710e-4145-97cd-1618da79e3e2": "Button",
    # CheckBox
    "35af3d93-d7c7-4a2e-a8eb-bac87a1a3f26": "CheckBoxField",
    # Calendar
    "e3c063d8-ef92-41be-9c89-b70290b5368b": "CalendarField",
    # Progress / splitter
    "b1db1f86-abbb-4cf0-8852-fe6ae21650c2": "ProgressBarField",
    "36e52348-5d60-4770-8e89-a16ed50a2006": "Splitter",
    # Picture field
    "151ef23e-6bb2-4681-83d0-35bc2217230c": "PictureField",
    # Scroll bar (полоса регулирования)
    "6c06cd5d-8481-4b6f-a90a-7a97a8bb8bef": "TrackBarField",
    # Graphical schema / chart / pivot chart / geo schema / HTML document
    "42248403-7748-49da-b782-e4438fd7bff3": "GraphicalSchemaField",
    "a8b97779-1a4b-4059-b09c-807f86d2a461": "ChartField",
    "a26da99e-184a-4823-b0d6-62816d38dc4e": "PivotChartField",
    "ad37194e-555e-4305-b718-5dca84baf145": "GeographicalSchemaField",
    "d92a805c-98ae-4750-9158-d9ce7cec2f20": "HTMLDocumentField",
    # Columns / column groups
    "48a6ebc3-fcc8-4f8f-b399-459fb32aa46b": "ColumnGroup",
    # Command panels
    "b78f2e80-ec68-11d4-9dcf-0050bae2bc79": "AutoCommandBar",
}

# ---------------------------------------------------------------------------
# Well-known system UUIDs (not controls)
# ---------------------------------------------------------------------------

SYSTEM_UUIDS = {
    "09ccdc77-ea1a-4a6d-ab1c-3435eada2433": "form_hierarchy_root",
    "59d6c227-97d3-46f6-84a0-584c5a2807e1": "form_properties_block",
    "b8621d8f-7502-46a6-b358-063a307d0c2d": "form_trailing_metadata",
    "4b7f5926-3927-48c8-a955-c1b03658f9c4": "form_event_handlers",
    "b78f2e80-ec68-11d4-9dcf-0050bae2bc79": "command_panel_container",
    "c1603291-0b6a-441e-9fba-1390eea0d99c": "command_panel_instance",
    "f3e46387-8590-46dc-89a0-dfcd62bc3012": "button_instance",
    "e1692cc2-605b-4535-84dd-28440238746c": "handler_uuid",
    "00000000-0000-0000-0000-000000000000": "null_uuid",
}

# ---------------------------------------------------------------------------
# Form type IDs (root block first number)
# ---------------------------------------------------------------------------

FORM_TYPE_NAMES: Dict[int, str] = {
    26: "ordinary_form_v26",
    27: "ordinary_form_v27",
}

# ---------------------------------------------------------------------------
# Event type IDs → event name (Russian / English)
# ---------------------------------------------------------------------------

EVENT_TYPE_MAP: Dict[int, tuple[str, str]] = {
    70000: ("ПередОткрытием", "BeforeOpen"),
    70001: ("ПриОткрытии", "OnOpen"),
    70002: ("ПередЗакрытием", "BeforeClose"),
    70003: ("ПриЗакрытии", "OnClose"),
    70009: ("ОбновлениеОтображения", "OnDisplayUpdate"),
    80000: ("ПередЗаписью", "BeforeWrite"),
    2147483647: ("ОбработчикЭлемента", "ElementHandler"),  # generic element-level
}

# ---------------------------------------------------------------------------
# Event type IDs → managed-form XML event name (spec §6)
# ---------------------------------------------------------------------------

FORM_EVENT_XML_NAMES: Dict[int, str] = {
    70000: "OnCreateAtServer",  # "before open" = server creation/init
    70001: "OnOpen",
    70002: "BeforeClose",
    70003: "OnClose",
    70009: "OnDisplayUpdate",
    80000: "BeforeWrite",
    80001: "OnWrite",
    80002: "AfterWrite",
}

# ---------------------------------------------------------------------------
# Element-level event IDs (block {21,[event_id, uuid, {3,"Method",...}]})
# → event name (Russian / English)
#
# Observed on a table field with 20 bound handlers. The same IDs apply to any
# element-level handler block.
# ---------------------------------------------------------------------------

ELEMENT_EVENT_ID_MAP: Dict[int, tuple[str, str]] = {
    34: ("Выбор", "Choice"),
    35: ("ПриАктивизацииСтроки", "OnActivateRow"),
    36: ("ПриАктивизацииКолонки", "OnActivateColumn"),
    37: ("ПриАктивизацииЯчейки", "OnActivateCell"),
    40: ("ПередНачаломДобавления", "BeforeAddRow"),
    41: ("ПередНачаломИзменения", "BeforeRowChange"),
    42: ("ПередУдалением", "BeforeDeleteRow"),
    43: ("ПриНачалеРедактирования", "OnStartEditing"),
    44: ("ПередОкончаниемРедактирования", "BeforeEditEnd"),
    45: ("ПриИзмененииФлажка", "OnCheckBoxChange"),
    47: ("ПриВыводеСтроки", "OnDrawRow"),
    48: ("ВыборЗначения", "ValueChoice"),
    49: ("ПриОкончанииРедактирования", "OnEditEnd"),
    50: ("ОбработкаЗаписиНовогоОбъекта", "NewObjectWriteProcessing"),
    51: ("ПослеУдаления", "AfterDeleteRow"),
    52: ("ОбработкаВыбора", "ChoiceProcessing"),
    53: ("ПриПолученииДанных", "OnGetData"),
    900: ("НачалоПеретаскивания", "DragStart"),
    901: ("ПроверкаПеретаскивания", "DragCheck"),
    902: ("ОкончаниеПеретаскивания", "EndDrag"),
    903: ("Перетаскивание", "Drag"),
}

# ---------------------------------------------------------------------------
# Element-level event names (2147483647 type) by handler pattern
# The actual event name is embedded in the handler description
# ---------------------------------------------------------------------------

ELEMENT_EVENT_PATTERNS: Dict[str, str] = {
    "НачалоВыбора": "StartChoice",
    "ПриИзменении": "OnChange",
    "Нажатие": "Click",
    "ОбработкаВыбора": "ChoiceProcessing",
    "ОбработкаВыбораЗначения": "ChoiceProcessing",
    "ПередНачаломДобавления": "BeforeAddRow",
    "ПослеУдаленияСтроки": "AfterDeleteRow",
    "ПередУдалениемСтроки": "BeforeDeleteRow",
    "ПриАктивизацииСтроки": "OnActivateRow",
    "ПередНачаломИзменения": "BeforeRowChange",
    "ОкончаниеПеретаскивания": "EndDrag",
    "НачалоПеретаскивания": "DragStart",
    "Перетаскивание": "Drag",
    "ПроверкаПеретаскивания": "DragCheck",
    "ОбработкаНавигационнойСсылки": "URLProcessing",
    "Очистка": "Clearing",
    "АвтоПодбор": "AutoComplete",
    "ОкончаниеВводаТекста": "TextEditEnd",
    "Открытие": "Opening",
    "ПриОткрытии": "OnOpen",
    "ПриИзмененииСтроки": "OnChange",
    "ПриИзмененииТипа": "OnChange",
    "Выбор": "Selection",
    "ПриВыводеСтроки": "OnPeriodOutput",
    "ОкончаниеРедактирования": "OnEditEnd",
}

# ---------------------------------------------------------------------------
# Managed-form attribute ``#`` type UUID resolution
#
# The binary stores reference/composite types as ``#<uuid>``. Without the
# configuration metadata the exact object name is unknown, so callers may
# register known UUIDs here. Unresolved types default to ``xs:anyType``.
# ---------------------------------------------------------------------------

ATTRIBUTE_TYPE_UUID_RESOLVER: Dict[str, str] = {
    # '66337e89-ea8c-4b37-8932-ff96cce601fd': 'cfg:DocumentObject.ПлановаяНагрузка',
}


def resolve_type_uuid(type_uuid: str) -> Optional[str]:
    """Resolve a ``#`` type UUID to a cfg: reference, or None."""
    return ATTRIBUTE_TYPE_UUID_RESOLVER.get(type_uuid)

# ---------------------------------------------------------------------------
# Attribute type patterns → 1C type string / XML type
# ---------------------------------------------------------------------------

ATTRIBUTE_TYPE_MAP: Dict[str, tuple[str, str]] = {
    # pattern_key → (1C type name, XML xs:type)
    "S":    ("Строка",           "xs:string"),
    "B":    ("Булево",           "xs:boolean"),
    "D":    ("Дата",             "xs:dateTime"),
    "N":    ("Число",            "xs:decimal"),
    "U":    ("УникальныйИдентификатор", "v8:UUID"),
}

# For complex types referenced by UUID, the XML type is "cfg:<MetadataType>"
# For Pattern with no sub-type, it's a generic variant

# ---------------------------------------------------------------------------
# Property block type IDs (inside {3,...} and {6,...} blocks)
# ---------------------------------------------------------------------------

# These are heuristic labels for common property patterns
PROPERTY_BLOCK_LABELS: Dict[int, str] = {
    0: "property_group",
    1: "alignment",
    2: "data_binding",
    3: "value",
    4: "font",
    5: "complex_property",
}

# ---------------------------------------------------------------------------
# Layout constraint anchor sides
# ---------------------------------------------------------------------------

ANCHOR_SIDES: Dict[int, str] = {
    -1: "none",
    0: "left",
    1: "top",
    2: "right",
    3: "bottom",
    6: "auto",  # sentinel {2,-1,6,0}
}

# ---------------------------------------------------------------------------
# Hierarchy sentinel
# ---------------------------------------------------------------------------

NO_PARENT = 4294967295  # 0xFFFFFFFF
NULL_UUID = "00000000-0000-0000-0000-000000000000"


def resolve_control_type(uuid: str) -> Optional[str]:
    """Resolve a control UUID to its XML tag name, or None if unknown."""
    return CONTROL_UUID_TO_XML_TAG.get(uuid)


def resolve_event_name(event_type_id: int) -> Optional[str]:
    """Resolve an event type ID to its Russian event name."""
    pair = EVENT_TYPE_MAP.get(event_type_id)
    return pair[0] if pair else None


def resolve_element_event(event_id: int) -> tuple[str, str]:
    """Resolve an element-level event ID to (Russian, English) names."""
    return ELEMENT_EVENT_ID_MAP.get(event_id, ("", ""))


def resolve_event_name_en(event_type_id: int) -> Optional[str]:
    """Resolve an event type ID to its English event name."""
    pair = EVENT_TYPE_MAP.get(event_type_id)
    return pair[1] if pair else None


def is_system_uuid(uuid: str) -> bool:
    """Check if a UUID is a well-known system UUID (not a control)."""
    return uuid in SYSTEM_UUIDS


def resolve_attribute_type(pattern: str) -> tuple[str, str]:
    """Resolve an attribute type pattern to (1C type, XML type)."""
    return ATTRIBUTE_TYPE_MAP.get(pattern, ("Произвольный", "xs:anyType"))
