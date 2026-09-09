"""Parser for 1C (BSL) source files: procedures/functions + call graph.

Design notes
------------
* 1C has a flat global namespace: procedures declared with `Экспорт` in any
  module (manager, object, form, external data processor) are callable by name
  from anywhere. We therefore resolve calls **by name** and store edges to all
  matching symbols (multiple candidates is normal in 1C).
* Calls are detected as identifiers followed by `(` outside strings/comments,
  excluding BSL keywords and the declaration line itself.
* Configuration object references are detected via prefix patterns:
  Справочники.X, Документы.Y, Константы.Z, Перечисления.W, РегистрыНакопления.R, ...

The parser is regex/line-based (not a full BSL grammar) — it is fast and good
enough for building a call graph over a whole configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- keywords ---

BSL_KEYWORDS = {
    "Если", "Тогда", "Иначе", "ИначеЕсли", "КонецЕсли",
    "Для", "Цикл", "Пока", "По", "Каждому", "Прервать", "Продолжить", "КонецЦикла",
    "ВызватьИсключение", "Возврат", "Перейти", "Попытка", "Исключение", "КонецПопытки",
    "Сообщить", "ЗаписатьЖурналРегистрации", "Выбрать", "Выгрузить",
    "ЕслиИ", "Или", "И", "Не", "Истинно", "Ложь",
    "Процедура", "Функция", "КонецПроцедуры", "КонецФункции",
    "Экспорт", "Атрибут", "Видимый", "НеВидимый",
    "Новый", "Тип", "Значение", "Строка", "Число", "Дата", "Булево", "ПериодВремени",
    "ДатаВремя", "Символ", "ДвоичныеДанные", "НаборСтрок", "Массив", "Соответствие",
    "Структура", "СписокЗначений", "ДокументСсылка", "КаталогСсылка", "Запрос",
    "ТабличнаяЧасть", "ТаблицаЗначений", "Файл", "Папка", "ДвижокЗапросов",
    "КомандаПользователя", "ПараметрыПриложения", "Сеанс", "Сервер",
}

# ------------------------------------------------------------------ patterns ---

_PROC_RE = re.compile(
    r"^\s*(Процедура|Функция)\s+(?P<name>[A-Za-zА-Яа-яЁё_][A-Za-z0-9А-Яа-яёЁ_\-]*)\s*"
    r"\((?P<params>[^)]*)\)\s*(?P<export>Экспорт)?\s*$",
    re.IGNORECASE,
)

_CALL_RE = re.compile(r"\b([A-Za-zА-Яа-яЁё_][A-Za-z0-9А-Яа-яёЁ_\-]*)\s*\(")

# configuration object references by prefix
_OBJ_REF_RE = re.compile(
    r"\b(Справочники|Документы|Константы|Перечисления|ПланыСчетов|ПланыВидовХарактеристик|"
    r"ПланыОбмена|РегистрыНакопления|РегистрыСведений|РегистрыБухгалтерии|РегистрыРасчётов|"
    r"Подсистемы|Обработки|Отчеты)\s*\.\s*"
    r"(?P<obj>[A-Za-zА-Яа-яЁё_][A-Za-z0-9А-Яа-яёЁ_\-]*)"
)

_STRING_RE = re.compile(r'"(?:[^"]|"")*"')

# method chains that are not configuration objects
_NOT_OBJECTS = {
    "НайтиПоСсылке", "НайтиПоКоду", "НайтиПоНаименованию", "НайтиПоРеквизиту",
    "СоздатьДокумент", "СоздатьЭлемент", "Ссылка", "НаборСсылок", "Выбрать",
    "ПолучитьОбъект", "Записать", "СписокЗначений",
}


@dataclass
class BSSymbol:
    """A procedure or function declared in a .bs file."""

    name: str
    kind: str                 # "procedure" | "function"
    visibility: str           # "global" (Экспорт) | "local"
    file: Path
    module_role: str          # manager | object | form:<formname> | library
    object_name: str          # owning configuration object, e.g. "Каталог.Номенклатура"
    line: int
    params: str = ""
    body: str = ""            # truncated for embedding
    calls: set[str] = field(default_factory=set)  # callee names invoked inside this procedure
    symbol_id: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.object_name}.{self.name}"


@dataclass
class BSModule:
    """Parsed result of one .bs file."""

    path: Path
    module_role: str = ""
    object_name: str = ""
    symbols: list[BSSymbol] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)        # (callee name, line idx 0-based)
    object_refs: list[tuple[str, str]] = field(default_factory=list)  # (prefix, obj short name)
    decl_ranges: list[tuple[int, int, BSSymbol]] = field(default_factory=list)
    # (start_line_idx, end_line_idx inclusive, symbol) — for call attribution


def _strip_strings_and_comments(line: str) -> str:
    line = _STRING_RE.sub('""', line)
    idx = line.find("//")
    if idx >= 0:
        line = line[:idx]
    return line


def parse_bs_file(
    path: Path,
    module_role: str = "",
    object_name: str = "",
    max_body_chars: int = 4000,
) -> BSModule:
    module = BSModule(path=path, module_role=module_role, object_name=object_name)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return module

    lines = text.splitlines(keepends=True)
    line_offsets: list[int] = []
    off = 0
    for ln in lines:
        line_offsets.append(off)
        off += len(ln)

    n = len(lines)

    # pass 1: find declarations and their bodies (by scanning to matching end)
    i = 0
    while i < n:
        m = _PROC_RE.match(lines[i])
        if not m:
            i += 1
            continue
        kind = "procedure" if m.group(1).lower() == "процедура" else "function"
        end_marker = "конецпроцедуры" if kind == "procedure" else "конецфункции"
        j = i + 1
        while j < n:
            stripped = _strip_strings_and_comments(lines[j]).strip().lower()
            if stripped.startswith(end_marker):
                break
            j += 1
        end_j = min(j, n - 1)
        body = "".join(lines[i + 1 : end_j])
        sym = BSSymbol(
            name=m.group("name"),
            kind=kind,
            visibility="global" if m.group("export") else "local",
            file=path,
            module_role=module_role,
            object_name=object_name,
            line=i + 1,
            params=(m.group("params") or "").strip(),
            body=body[:max_body_chars],
        )
        module.symbols.append(sym)
        module.decl_ranges.append((i, end_j, sym))
        i = end_j + 1

    # pass 2: calls and object references (skip declaration lines)
    for i, raw in enumerate(lines):
        stripped = _strip_strings_and_comments(raw).strip()
        if not stripped:
            continue
        is_decl = bool(_PROC_RE.match(raw))
        # skip the line inside decl_ranges that is a "Конец..." terminator
        in_body = False
        enclosing: BSSymbol | None = None
        for start, end, sym in module.decl_ranges:
            if start <= i <= end:
                if i == start or i == end:
                    break  # declaration line or its terminator — no calls counted
                in_body = True
                enclosing = sym
                break
        if is_decl and not in_body:
            continue
        for cm in _CALL_RE.finditer(stripped):
            callee = cm.group(1)
            if callee in BSL_KEYWORDS:
                continue
            module.calls.append((callee, i))
            if enclosing is not None:
                enclosing.calls.add(callee)
        for om in _OBJ_REF_RE.finditer(stripped):
            obj_short = om.group("obj")
            if obj_short and obj_short not in _NOT_OBJECTS:
                module.object_refs.append((om.group(1), obj_short))

    return module


def infer_module_role(folder: Path, file_name: str) -> tuple[str, str]:
    """Infer (module_role, object_name) from the folder/file layout.

    Layout examples:
      Catalogs/Catalog.Nomenclature/ObjectModule.bsl   -> ("object", "Каталог.Номенклатура")
      Catalogs/Catalog.X/ManagerModule.bsl             -> ("manager", "Каталог.X")
      Catalogs/Catalog.Y/Forms/FormZ/FormModule.bsl    -> ("form:FormZ", "Каталог.Y")
    """
    name = file_name[:-4] if file_name.lower().endswith((".bsl", ".bs")) else file_name
    role = "module"
    if name.endswith("Module"):
        base = name[: -len("Module")]
        if base == "Manager":
            role = "manager"
        elif base == "Object":
            role = "object"
        elif base.startswith("Form"):
            role = f"form:{base[4:] or 'default'}"
        else:
            role = base.lower() or "module"

    # object name from folder path: <root>/<TypeFolder>/<Type>.<Name>/Module.bsl
    # the object folder is the immediate parent of the module file, e.g. "Catalog.Nomenclature"
    object_name = folder.name if "." in folder.name else ""
    if not object_name:
        for part in reversed(folder.parts):
            if "." in part:
                object_name = part
                break
    return role, object_name
