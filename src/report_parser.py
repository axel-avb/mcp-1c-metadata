"""Parser for ОтчетПоКонфигурации.txt (UTF-16LE tab-indented tree).

Produces the same ConfigObject/ConfigElement model as the deprecated XML parser.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config_parser import ConfigElement, ConfigObject

# Plural container segment -> (normalized type, singular prefix for object name)
_TYPE_MAP: dict[str, tuple[str, str]] = {
    "Справочники": ("catalog", "Справочник"),
    "Документы": ("document", "Документ"),
    "РегистрыНакопления": ("accumulation_register", "РегистрНакопления"),
    "РегистрыСведений": ("information_register", "РегистрСведений"),
    "ЖурналыРегистрации": ("registration_log", "ЖурналРегистрации"),
    "ОбщиеМодули": ("common_module", "ОбщийМодуль"),
    "СлужебныеОбщиеМодули": ("common_module", "ОбщийМодуль"),
    "Перечисления": ("enumeration", "Перечисление"),
    "Отчеты": ("report", "Отчет"),
    "Обработки": ("processing", "Обработка"),
    "ВнешниеОбработки": ("processing", "Обработка"),
    "КомандныеИнтерфейсы": ("command_interface", "КомандныйИнтерфейс"),
    "Роли": ("role", "Роль"),
    "Константы": ("constant", "Константа"),
    "ХранилищаНастроек": ("settings_storage", "ХранилищеНастроек"),
    "ПланыОбмена": ("exchange_plan", "ПланОбмена"),
    "ПланыВидовХарактеристик": ("characteristic_type_plan", "ПланВидовХарактеристики"),
    "РегламентныеЗадания": ("scheduled_job", "РегламентноеЗадание"),
    "Задачи": ("task", "Задача"),
    "ОбщиеФормы": ("common_form", "ОбщаяФорма"),
    "Языки": ("language", "Язык"),
    "Подсистемы": ("subsystem", "Подсистема"),
    "Интерфейсы": ("interface", "Интерфейс"),
    "Стили": ("style", "Стиль"),
    "Команды": ("command", "Команда"),
}

# Element container segment -> elem_type
_ELEM_TYPE_MAP: dict[str, str] = {
    "Реквизиты": "attribute",
    "ТабличныеЧасти": "tabular_section",
    "Команды": "command",
    "Формы": "form",
    "ПредопределенныеДанные": "predefined_data",
    "Характеристики": "characteristic",
    "Владелец": "owner",
    "Параметры": "parameter",
    "Ресурсы": "resource",
    "Измерения": "dimension",
    "ВидыХарактеристик": "characteristic_type",
    "Значения": "value",
    "КомпоновкиДанных": "composition",
    "Макеты": "layout",
    "Шаблоны": "template",
    "События": "event",
    "Правила": "rule",
    "КомпоновкаДанных": "composition",
}

# Data type patterns
_REF_TYPE_RE = re.compile(
    r"^(СправочникСсылка|ДокументСсылка|ПеречислениеЗначение|ПеречислениеСсылка|"
    r"РегистрСведенийСсылка|РегистрНакопленияСсылка|"
    r"ЖурналРегистрацииСсылка|КонстантаСсылка|"
    r"ХранилищеНастроекСсылка|ПланОбменаСсылка|"
    r"ПланВидовХарактеристикСсылка|ЗадачаСсылка)"
    r"[.(](.+)$"
)
_SIMPLE_TYPES = {
    "Строка", "Число", "Дата", "Булево", "ДатаВремя", "ДвоичныеДанные",
    "URL", "Числовой", "Валюта", "Период", "НаборСвойств", "ОписаниеДанных",
    "СправочникСсылка", "ДокументСсылка", "ПеречислениеЗначение",
    "ЭлементНабораСвойств", "Структура", "Функция", "ВидДанных",
    "ХранилищеДанных", "ТипЗнания", "Файл", "СхемаДанных", "ОбластьДанных",
    "СвойствоДанных", "ОписаниеДанных", "ИерархияЗначений", "Диапазон",
}


def _parse_data_type(raw: str) -> tuple[str, str]:
    """Parse a Тип property value into (data_type, ref_object)."""
    raw = raw.strip().strip('"')
    m = _REF_TYPE_RE.match(raw)
    if m:
        ref_prefix = m.group(1)
        ref_name = m.group(2)
        # Map ref prefix to singular object type
        ref_map = {
            "СправочникСсылка": "Справочник",
            "ДокументСсылка": "Документ",
            "ПеречислениеЗначение": "Перечисление",
            "ПеречислениеСсылка": "Перечисление",
            "РегистрСведенийСсылка": "РегистрСведений",
            "РегистрНакопленияСсылка": "РегистрНакопления",
            "ЖурналРегистрацииСсылка": "ЖурналРегистрации",
            "КонстантаСсылка": "Константа",
            "ХранилищеНастроекСсылка": "ХранилищеНастроек",
            "ПланОбменаСсылка": "ПланОбмена",
            "ПланВидовХарактеристикСсылка": "ПланВидовХарактестик",
            "ЗадачаСсылка": "Задача",
        }
        singular = ref_map.get(ref_prefix, ref_prefix)
        return raw, f"{singular}.{ref_name}"
    # Simple type or parameterized
    return raw, ""


class _Node:
    __slots__ = ("name", "indent", "props", "multi_list", "multi_key", "children")

    def __init__(self, name: str, indent: int):
        self.name = name
        self.indent = indent
        self.props: dict[str, str] = {}
        self.multi_list: list[str] | None = None
        self.multi_key: str = ""
        self.children: list[_Node] = []


def parse_report(path: str | Path) -> list[ConfigObject]:
    """Parse ОтчетПоКонфигурации.txt and return list of ConfigObject."""
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode("utf-16-le")
    lines = text.split("\n")

    objects: list[ConfigObject] = []
    # Stack of _Node for tree navigation
    stack: list[_Node] = []
    # Stack of (indent, is_object) for tracking context
    obj_stack: list[tuple[int, bool]] = []  # (indent, is_top_level_object)

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\r")
        i += 1
        if not line:
            continue

        # Count leading tabs
        indent = 0
        while indent < len(line) and line[indent] == "\t":
            indent += 1
        content = line[indent:]

        if content.startswith("- "):
            # New node
            node_name = content[2:].strip()
            node = _Node(node_name, indent)

            # Pop stack to find parent
            while stack and stack[-1].indent >= indent:
                stack.pop()

            if stack:
                stack[-1].children.append(node)
            stack.append(node)

            # Determine if this is a top-level object
            first_segment = node_name.split(".", 1)[0]
            if first_segment in _TYPE_MAP and indent <= 2:
                # This is a configuration object
                obj = _build_object(node, lines, i, indent)
                if obj is not None:
                    objects.append(obj)
                    # Skip lines that were consumed
                    i = obj._consumed_to  # type: ignore[attr-defined]
                    # Pop stack entries we added
                    while stack and stack[-1].indent >= indent:
                        stack.pop()
                    stack.append(node)
            elif first_segment in _TYPE_MAP:
                # Nested object (shouldn't happen normally, but be safe)
                pass

        elif ":" in content:
            # Property line
            key, _, value = content.partition(":")
            key = key.strip()
            value = value.strip()

            if not stack:
                continue

            current = stack[-1]
            if value and value.startswith('"') and value.endswith('"'):
                current.props[key] = value[1:-1]
                current.multi_list = None
            elif value == "":
                # Start of multi-value list
                current.multi_key = key
                current.multi_list = []
            else:
                current.props[key] = value

        elif content.startswith('"') and content.endswith('"') and len(content) > 1:
            # Multi-value list item
            if stack and stack[-1].multi_list is not None:
                stack[-1].multi_list.append(content[1:-1])

    return objects


def _build_object(
    node: _Node, lines: list[str], start_i: int, obj_indent: int
) -> ConfigObject | None:
    """Build a ConfigObject from a node and its descendant lines."""
    first_segment = node.name.split(".", 1)[0]
    type_info = _TYPE_MAP.get(first_segment)
    if type_info is None:
        return None

    obj_type, singular_prefix = type_info
    obj_short_name = node.name.split(".", 1)[1] if "." in node.name else node.name
    obj_name = f"{singular_prefix}.{obj_short_name}"

    obj = ConfigObject(
        name=obj_name,
        type=obj_type,
    )

    # Parse properties and children from subsequent lines
    i = start_i
    n = len(lines)
    elem_stack: list[tuple[int, ConfigElement]] = []  # (indent, element)
    multi_key = ""
    multi_list: list[str] | None = None
    current_elem: ConfigElement | None = None

    while i < n:
        line = lines[i].rstrip("\r")
        i += 1
        if not line:
            continue

        indent = 0
        while indent < len(line) and line[indent] == "\t":
            indent += 1
        content = line[indent:]

        # If we've gone back to same or lesser indent than object, stop
        if indent <= obj_indent and not content.startswith("- "):
            break
        if indent < obj_indent:
            break

        if content.startswith("- "):
            child_name = content[2:].strip()

            # Check if this is a child object (another type) or an element
            child_first_seg = child_name.split(".", 1)[0] if "." in child_name else ""
            # Elements have the pattern: ObjectName.Container.ElemName
            # e.g. Справочники.Организации.Реквизиты.ТипОрганизации
            if child_first_seg == first_segment and obj_short_name in child_name:
                # This is an element of our object
                # Determine elem_type from the segment between object name and element name
                # Path after the object name alternates container/name, e.g.
                #   Реквизиты.ТипОрганизации                    -> attribute
                #   ТабличныеЧасти.Конкуренты.Реквизиты.ВУЗ     -> attribute inside a tabular section
                rest = child_name[len(first_segment) + 1 + len(obj_short_name) + 1:]
                segs = rest.split(".")
                elem_name = segs[-1]

                # type comes from the last known container segment in the path
                elem_type = ""
                for seg in reversed(segs[:-1]):
                    if seg in _ELEM_TYPE_MAP:
                        elem_type = _ELEM_TYPE_MAP[seg]
                        break
                if not elem_type:
                    elem_type = segs[-2] if len(segs) > 1 else ""

                # Close multi-list if open
                if multi_list is not None:
                    if current_elem is not None:
                        current_elem.properties[multi_key] = "\n".join(multi_list)
                    multi_list = None
                    multi_key = ""

                elem = ConfigElement(
                    name=elem_name,
                    elem_type=elem_type,
                )

                # Pop stack entries at this indent or deeper; the parent is
                # whatever remains on top (or the object itself when empty).
                while elem_stack and elem_stack[-1][0] >= indent:
                    elem_stack.pop()
                if elem_stack:
                    elem_stack[-1][1].children.append(elem)
                else:
                    obj.elements.append(elem)

                elem_stack.append((indent, elem))
                current_elem = elem
            else:
                # Another object at same level or different type — stop
                if indent <= obj_indent:
                    break
                # Nested object (subsystem composition etc.) — skip
                # Consume its subtree
                i = _skip_subtree(lines, i, indent)
                continue

        elif ":" in content:
            key, _, value = content.partition(":")
            key = key.strip()
            value = value.strip()

            if value and value.startswith('"') and value.endswith('"'):
                if multi_list is not None and current_elem is not None:
                    current_elem.properties[multi_key] = "\n".join(multi_list)
                val = value[1:-1]
                _assign_prop(obj, current_elem, key, val)
                multi_list = None
                multi_key = ""
            elif value == "":
                multi_key = key
                multi_list = []
            else:
                if multi_list is not None and current_elem is not None:
                    current_elem.properties[multi_key] = "\n".join(multi_list)
                _assign_prop(obj, current_elem, key, value)
                multi_list = None
                multi_key = ""

        elif content.startswith('"') and content.endswith('"') and len(content) > 1:
            if multi_list is not None:
                multi_list.append(content[1:-1])

    # Flush last multi-list
    if multi_list is not None and current_elem is not None:
        current_elem.properties[multi_key] = "\n".join(multi_list)

    # Post-process: extract Тип from properties into data_type/ref_object
    _process_object_properties(obj)

    # Store consumed line index for the caller
    obj._consumed_to = i - 1  # type: ignore[attr-defined]
    return obj


def _skip_subtree(lines: list[str], start_i: int, subtree_indent: int) -> int:
    """Skip lines that belong to a subtree at the given indent. Returns next line index."""
    i = start_i
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\r")
        i += 1
        if not line:
            continue
        indent = 0
        while indent < len(line) and line[indent] == "\t":
            indent += 1
        if indent <= subtree_indent:
            break
    return i - 1


def _assign_prop(
    obj: ConfigObject,
    elem: ConfigElement | None,
    key: str,
    value: str,
) -> None:
    """Assign a property to the object or current element."""
    if key == "Имя":
        if elem is None:
            obj.name = f"{obj.name.split('.', 1)[0]}.{value}"
        else:
            elem.name = value
    elif key == "Синоним":
        if elem is None:
            obj.synonym = value
        else:
            elem.synonym = value
    elif key == "Комментарий":
        if elem is None:
            obj.comment = value
        else:
            elem.comment = value
    elif key == "Тип" and elem is not None:
        data_type, ref_object = _parse_data_type(value)
        elem.data_type = data_type
        elem.ref_object = ref_object
    else:
        if elem is not None:
            elem.properties[key] = value
        else:
            # Store in object (no generic props on ConfigObject, use elements)
            pass


def _process_object_properties(obj: ConfigObject) -> None:
    """Post-process: refine element data types and ref objects."""
    for elem in obj.elements:
        _process_element(elem)


def _process_element(elem: ConfigElement) -> None:
    # If Тип was stored as raw property, parse it
    if "Тип" in elem.properties and not elem.data_type:
        raw = elem.properties["Тип"]
        data_type, ref_object = _parse_data_type(raw)
        elem.data_type = data_type
        elem.ref_object = ref_object
    for child in elem.children:
        _process_element(child)
