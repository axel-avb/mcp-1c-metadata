"""
Deep recursive parser for 1C Form.bin curly-brace form structure text.

Parses the curly-brace text format into a structured AST (ParsedForm) with:
- Controls (with UUID, type, name, coordinates, properties)
- Hierarchy (parent-child relationships)
- Attributes (form-level data attributes)
- Commands (form commands with titles and actions)
- Events (form and control-level event bindings)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from parsers.form_type_mapper import (
    ANCHOR_SIDES,
    CONTROL_UUID_TO_XML_TAG,
    EVENT_TYPE_MAP,
    FORM_TYPE_NAMES,
    NO_PARENT,
    SYSTEM_UUIDS,
    is_system_uuid,
    resolve_attribute_type,
    resolve_control_type,
    resolve_element_event,
    resolve_event_name,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AST data classes
# ---------------------------------------------------------------------------

@dataclass
class FormEvent:
    """An event binding (form-level or control-level)."""
    event_type_id: int = 0
    event_name_ru: str = ""
    event_name_en: str = ""
    handler_method: str = ""
    control_index: int = -1  # -1 = form-level


@dataclass
class FormColumn:
    """A column of a table control (value table / spreadsheet)."""
    uuid: str = ""
    name: str = ""
    title: str = ""
    type_pattern: str = ""
    type_uuid: str = ""
    type_1c: str = ""
    type_xml: str = ""
    type_length: int = 0


@dataclass
class FormControl:
    """A control (element) on the form."""
    index: int = 0
    uuid: str = ""
    control_type_uuid: str = ""
    xml_tag: str = ""  # resolved XML tag name
    name: str = ""  # element name (managed: from {14,"Name",...}; ordinary: localized/generated)
    element_name: str = ""  # managed-style element name from {14,"Name",...}
    title: str = ""  # localized display title (e.g. {1,2,{"ru","Номер:"},...})
    tooltip: str = ""  # localized tooltip / comment for the control
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    command_name: str = ""  # from {14,"name",...} in ordinary forms (button command)
    data_path: str = ""
    data_attribute_index: int = -1
    data_binding_uuid: str = ""  # attribute ref uuid from BLK[9] [1,[uuid,...]]
    type_pattern: str = ""  # "S", "N", "D", "B", "#UUID", "Pattern"
    type_uuid: str = ""
    type_1c: str = ""
    type_xml: str = ""
    type_length: int = 0  # string length for "S" pattern
    choice_list: List[Dict[str, str]] = field(default_factory=list)  # [{value, title}]
    columns: List[FormColumn] = field(default_factory=list)  # table columns
    properties: Dict[str, Any] = field(default_factory=dict)
    events: List[FormEvent] = field(default_factory=list)
    raw_blocks: List[Any] = field(default_factory=list)  # raw parsed blocks


@dataclass
class FormAttribute:
    """A form-level data attribute (variable)."""
    index: int = 0
    uuid: str = ""
    flags: int = 0
    direction: int = 0  # 0=hidden, 1=visible
    name: str = ""
    type_pattern: str = ""  # "S", "B", "D", "#UUID", "Pattern"
    type_uuid: str = ""
    type_1c: str = ""
    type_xml: str = ""
    type_length: int = 0  # string length for "S" pattern


@dataclass
class FormDataBinding:
    """Maps a control index to an attribute index."""
    control_index: int = 0
    attribute_index: int = 0


@dataclass
class FormCommand:
    """A form command (action) with its display title."""
    name: str = ""
    title: str = ""
    action: str = ""


@dataclass
class ParsedForm:
    """Complete parsed structure of a 1C ordinary form."""
    form_type: int = 0
    form_type_name: str = ""
    title: str = ""
    width: int = 0
    height: int = 0
    main_attribute: str = ""
    controls: List[FormControl] = field(default_factory=list)
    hierarchy: List[Tuple[int, int]] = field(default_factory=list)  # (child, parent)
    attributes: List[FormAttribute] = field(default_factory=list)
    data_bindings: List[FormDataBinding] = field(default_factory=list)
    commands: List[FormCommand] = field(default_factory=list)
    events: List[FormEvent] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)
    tree: Optional[FormTreeNode] = None  # synthetic control tree (build_tree)
    _command_names: set = field(default_factory=set, repr=False)


@dataclass
class FormTreeNode:
    """A node in the synthetic control tree (from build_tree)."""
    index: int = 0
    name: str = ""
    ctrl: Optional[FormControl] = None  # None => container group
    kind: str = "leaf"  # "group" | "leaf"
    children: List['FormTreeNode'] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Token types for the lexer
# ---------------------------------------------------------------------------

class _TT:
    LBRACE = "LBRACE"
    RBRACE = "RBRACE"
    COMMA = "COMMA"
    INTEGER = "INTEGER"
    STRING = "STRING"
    FLOAT = "FLOAT"
    UUID = "UUID"
    EOF = "EOF"


@dataclass
class _Token:
    type: str
    value: Any
    pos: int = 0


# ---------------------------------------------------------------------------
# Lexer: curly-brace text → tokens
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)
_INT_RE = re.compile(r'-?\d+')
_FLOAT_RE = re.compile(r'-?\d+\.\d+')


def _tokenize(text: str) -> List[_Token]:
    """Tokenize curly-brace form structure text into a flat token list."""
    tokens: List[_Token] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == '{':
            tokens.append(_Token(_TT.LBRACE, '{', i))
            i += 1
        elif ch == '}':
            tokens.append(_Token(_TT.RBRACE, '}', i))
            i += 1
        elif ch == ',':
            tokens.append(_Token(_TT.COMMA, ',', i))
            i += 1
        elif ch == '"':
            # String literal
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == '\\':
                    j += 1  # skip escaped char
                j += 1
            tokens.append(_Token(_TT.STRING, text[i + 1:j], i))
            i = j + 1
        elif ch in (' ', '\t', '\r', '\n'):
            i += 1
        elif ch == '#':
            # base64 blob — skip to closing brace
            j = i + 1
            while j < n and text[j] != '}':
                j += 1
            tokens.append(_Token(_TT.STRING, text[i:j], i))
            i = j
        else:
            # Number or UUID (match at offset to avoid O(n^2) slicing)
            m = _UUID_RE.match(text, i)
            if m:
                tokens.append(_Token(_TT.UUID, m.group(0).lower(), i))
                i = m.end()
                continue
            m = _FLOAT_RE.match(text, i)
            if m:
                tokens.append(_Token(_TT.FLOAT, float(m.group(0)), i))
                i = m.end()
                continue
            m = _INT_RE.match(text, i)
            if m:
                tokens.append(_Token(_TT.INTEGER, int(m.group(0)), i))
                i = m.end()
                continue
            # Unknown char — skip
            i += 1

    tokens.append(_Token(_TT.EOF, None, len(text)))
    return tokens


# ---------------------------------------------------------------------------
# Recursive-descent parser: tokens → nested Python objects
# ---------------------------------------------------------------------------

class _Parser:
    """Recursive-descent parser for curly-brace token stream."""

    def __init__(self, tokens: List[_Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> _Token:
        return self.tokens[self.pos]

    def advance(self) -> _Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, tt: str) -> _Token:
        tok = self.advance()
        if tok.type != tt:
            raise ValueError(
                f"Expected {tt} at pos {tok.pos}, got {tok.type} ({tok.value!r})"
            )
        return tok

    def parse_value(self) -> Any:
        """Parse a single value (atom or braced block)."""
        tok = self.peek()
        if tok.type == _TT.LBRACE:
            return self.parse_brace()
        elif tok.type == _TT.RBRACE:
            # End of current block — caller handles
            return None
        elif tok.type == _TT.COMMA:
            # Empty element in list — return None
            return None
        else:
            return self.advance().value

    def parse_brace(self) -> list:
        """Parse { ... } into a list of values."""
        self.expect(_TT.LBRACE)
        items: list = []
        while self.peek().type not in (_TT.RBRACE, _TT.EOF):
            val = self.parse_value()
            items.append(val)
            if self.peek().type == _TT.COMMA:
                self.advance()  # consume comma
        self.expect(_TT.RBRACE)
        return items

    def parse_all(self) -> list:
        """Parse the entire token stream into a nested list structure."""
        result = []
        while self.peek().type != _TT.EOF:
            val = self.parse_value()
            if val is not None:
                result.append(val)
        return result


# ---------------------------------------------------------------------------
# High-level extraction from nested list AST
# ---------------------------------------------------------------------------

def _find_localized_name(obj: Any, _depth: int = 0) -> str:
    """Extract a Russian localized name from a {1,N,{"ru","..."},...} block.

    Deeply recursive: localized titles are often nested several levels inside
    control definition blocks. Accepts any language count N >= 1 (managed forms
    typically use {1,2,{"ru",...},{"en",...}} while ordinary forms use {1,1,...}).
    """
    if not isinstance(obj, list) or _depth > 16:
        return ""
    for item in obj:
        if isinstance(item, list):
            # Check for {1,N,{"ru","..."},...} with N >= 1
            if (len(item) >= 3 and isinstance(item[0], int) and item[0] == 1
                    and isinstance(item[1], int) and item[1] >= 1):
                for sub in item[2:]:
                    if isinstance(sub, list) and len(sub) >= 2:
                        if sub[0] == "ru" and isinstance(sub[1], str):
                            return sub[1]
            found = _find_localized_name(item, _depth + 1)
            if found:
                return found
    return ""


def _is_loc_pair(item: Any) -> bool:
    """True for a {1,N,{"ru","..."},...} localized pair with N >= 1."""
    return (isinstance(item, list) and len(item) >= 3
            and isinstance(item[0], int) and item[0] == 1
            and isinstance(item[1], int) and item[1] >= 1
            and isinstance(item[2], list) and len(item[2]) >= 2
            and item[2][0] == "ru" and isinstance(item[2][1], str))


def _is_layout_marker(item: Any) -> bool:
    """True for a [16, <int>, ...] layout/props marker block."""
    return (isinstance(item, list) and len(item) >= 2
            and item[0] == 16 and isinstance(item[1], int))


def _find_nested_block(obj: Any, marker: int) -> Optional[list]:
    """Return the first nested block whose first element equals ``marker``."""
    if not isinstance(obj, list):
        return None
    if obj and obj[0] == marker:
        return obj
    for item in obj:
        if isinstance(item, list):
            found = _find_nested_block(item, marker)
            if found is not None:
                return found
    return None


def _extract_title_tooltip(obj: Any) -> Tuple[str, str]:
    """Extract a control's display title and tooltip/comment.

    A control's props live in a list that holds a ``[16, N, ...]`` layout
    marker block. The title is a ``{1,1,{"ru",...}}`` sibling of that marker;
    the tooltip/comment is a ``{1,1,{"ru",...}}`` nested INSIDE the marker
    block. Returns (title, tooltip).
    """
    found: Dict[str, str] = {"title": "", "tooltip": ""}

    def visit(node: Any):
        if isinstance(node, list):
            # Locate the layout marker and treat ``node`` as the props list.
            for i, el in enumerate(node):
                if _is_layout_marker(el):
                    # tooltip: first localized string inside the marker block
                    if not found["tooltip"]:
                        for sub in el:
                            if _is_loc_pair(sub):
                                found["tooltip"] = sub[2][1]
                                break
                    # title: first localized sibling of the marker in node
                    if not found["title"]:
                        for sib in node[:i] + node[i + 1:]:
                            if _is_loc_pair(sib):
                                found["title"] = sib[2][1]
                                break
                    break
            for el in node:
                if isinstance(el, list):
                    visit(el)

    visit(obj)
    return found["title"], found["tooltip"]



def _find_command_title(obj: Any, _depth: int = 0) -> str:
    """Find a command's display title inside a {7,...} command block.

    The title is a {1,1,{"ru",...}} block nested inside the command's
    {3,"Action",...} definition. Unlike ``_find_control_title`` this descends
    into command-definition subtrees.
    """
    if not isinstance(obj, list) or _depth > 16:
        return ""
    for item in obj:
        if not isinstance(item, list):
            continue
        # Direct {1,1,{"ru","..."}} title block.
        if (len(item) >= 3 and isinstance(item[0], int) and item[0] == 1
                and isinstance(item[1], int) and item[1] >= 1):
            for sub in item[2:]:
                if isinstance(sub, list) and len(sub) >= 2:
                    if sub[0] == "ru" and isinstance(sub[1], str):
                        return sub[1]
        found = _find_command_title(item, _depth + 1)
        if found:
            return found
    return ""


def _is_uuid(val: Any) -> bool:
    """Check if a value is a UUID string."""
    return isinstance(val, str) and len(val) == 36 and '-' in val


def _extract_uuid_from_brace(obj: Any) -> Optional[str]:
    """If obj is a list starting with a UUID, return it."""
    if isinstance(obj, list) and obj and _is_uuid(obj[0]):
        return obj[0]
    return None


def _find_coordinates(brace: list) -> Tuple[int, int, int, int]:
    """Find {8,L,T,R,B,...} coordinate block within a brace."""
    for item in brace:
        if isinstance(item, list) and len(item) >= 5 and item[0] == 8:
            return (int(item[1]), int(item[2]), int(item[3]), int(item[4]))
    return (0, 0, 0, 0)


def _find_command_name(brace: list) -> str:
    """Find {14,"command_name",...} block within a brace."""
    for item in brace:
        if isinstance(item, list) and len(item) >= 2 and item[0] == 14:
            if isinstance(item[1], str):
                return item[1]
    return ""


def _find_sub_brace(brace: list, start_type: int) -> Optional[list]:
    """Find a sub-brace whose first element equals start_type."""
    for item in brace:
        if isinstance(item, list) and item and item[0] == start_type:
            return item
    return None


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class FormStructureParser:
    """
    Parses 1C Form.bin curly-brace form structure text into ParsedForm AST.

    Usage:
        parser = FormStructureParser()
        parsed = parser.parse(form_structure_text)
    """

    def parse(self, text: str) -> ParsedForm:
        """Parse curly-brace form structure text into ParsedForm."""
        if not text or not text.strip():
            return ParsedForm()

        # Tokenize and parse into nested list AST
        tokens = _tokenize(text)
        rd = _Parser(tokens)
        ast = rd.parse_all()

        if not ast:
            return ParsedForm()

        # The AST should be a single top-level list: [{form_type_id, ...}]
        if isinstance(ast[0], list):
            root = ast[0]
        else:
            root = ast

        return self._extract_form(root)

    def _extract_form(self, root: list) -> ParsedForm:
        """Extract ParsedForm from the root AST node."""
        result = ParsedForm()

        if not root:
            return result

        # First element is the form type ID
        if isinstance(root[0], int):
            result.form_type = root[0]
            result.form_type_name = FORM_TYPE_NAMES.get(root[0], f"unknown_{root[0]}")
        self._form_type = result.form_type

        # Scan root for all components
        self._extract_root_components(root, result)

        return result

    def _extract_root_components(self, root: list, result: ParsedForm):
        """
        Extract all components from the root list.

        Root structure: [form_type, layout_block, attrs_or_flags, ...]
        Layout structure: [16, title_block, hierarchy_uuid_block, w, h, flags...]
        Hierarchy block: [UUID, {1,...inner...}, {5, ctrl1, ctrl2, ...}]
        """
        if len(root) < 2:
            return

        # The layout block is root[1]
        layout = root[1]
        if not isinstance(layout, list) or len(layout) < 3:
            return

        # Extract title from layout[1]
        title_block = layout[1]
        if isinstance(title_block, list):
            title = _find_localized_name(title_block)
            if title:
                result.title = title

        # Find the hierarchy UUID block inside layout
        for item in layout:
            if (isinstance(item, list) and len(item) >= 2
                    and item and _is_uuid(item[0])
                    and item[0] == "09ccdc77-ea1a-4a6d-ab1c-3435eada2433"):
                self._extract_from_hierarchy_block(item, result)
                break

        # Find form dimensions from layout
        self._find_form_dimensions(layout, result)

        # Find attributes section in root
        self._find_attributes_in_root(root, result)

        # Find form-level events in root
        self._find_events_in_root(root, result)

        # Detect the main (object) attribute
        self._detect_main_attribute(result)

    def _extract_from_hierarchy_block(self, block: list, result: ParsedForm):
        """
        Extract controls and hierarchy from the UUID hierarchy root block.

        Block structure:
        {UUID,
          {1, {commands, page_data...}, hierarchy_entries..., 0,0, page_controls..., w, h, ...},
          {5, ctrl1, ctrl2, ...}  <-- controls are in a type-5 wrapper
        }
        """
        if len(block) < 3:
            return

        inner = block[1]
        if not isinstance(inner, list) or not inner or inner[0] != 1:
            return

        # Hierarchy entries are inside inner[1] (the big data list)
        data_list = inner[1] if len(inner) > 1 and isinstance(inner[1], list) else []
        for item in data_list:
            if (isinstance(item, list) and len(item) == 3
                    and item[0] == 0 and isinstance(item[1], int)
                    and isinstance(item[2], int)):
                child, parent = item[1], item[2]
                if child != 0 or parent != 0:
                    result.hierarchy.append((child, parent))

        # Extract controls from the wrapper block (type 3 or 5)
        wrapper = block[2] if len(block) > 2 else None
        if isinstance(wrapper, list) and wrapper and isinstance(wrapper[0], int):
            for item in wrapper[1:]:
                if isinstance(item, list) and item and _is_uuid(item[0]):
                    if is_system_uuid(item[0]):
                        continue
                    ctrl = self._parse_control_block(item)
                    if ctrl:
                        result.controls.append(ctrl)
                        # Commands are nested in the control's own block.
                        for cmd in self._collect_commands(item):
                            if cmd.name not in result._command_names:
                                result._command_names.add(cmd.name)
                                result.commands.append(cmd)

    def _find_form_dimensions(self, layout: list, result: ParsedForm):
        """Find width/height from layout block integers."""
        # Layout = [16, title, hierarchy_block, width, height, flags...]
        # Width/height are at layout[3] and layout[4]
        if len(layout) > 4:
            for i in range(3, min(6, len(layout))):
                if isinstance(layout[i], int) and isinstance(layout[i + 1], int):
                    w, h = layout[i], layout[i + 1]
                    if 10 <= w <= 5000 and 10 <= h <= 5000:
                        result.width = w
                        result.height = h
                        break

    def _find_attributes_in_root(self, root: list, result: ParsedForm):
        """Find the attributes and data-binding sections in the root region."""
        if len(root) > 2 and isinstance(root[2], list):
            for item in root[2]:
                if isinstance(item, list) and self._looks_like_attribute_section(item):
                    self._parse_attribute_section(item, result)
                elif isinstance(item, list) and self._looks_like_binding_section(item):
                    self._parse_binding_section(item, result)
            if result.attributes or result.data_bindings:
                return
        # Fallback: recursive search for type-3 attribute sections
        self._recursive_find_attributes(root, result)

    def _looks_like_binding_section(self, item: Any) -> bool:
        """Detect a data-binding wrapper where entries are [ctrl_idx, [1,[attr_idx]]]."""
        if not isinstance(item, list) or len(item) < 2:
            return False
        if item[0] not in (1, 3, 16):
            return False
        for entry in item[1:]:
            if (isinstance(entry, list) and len(entry) == 2
                    and isinstance(entry[0], int) and isinstance(entry[1], list)
                    and entry[1] and entry[1][0] == 1
                    and isinstance(entry[1][1], list) and entry[1][1]):
                return True
        return False

    def _parse_binding_section(self, section: list, result: ParsedForm):
        """Parse binding entries into FormDataBinding.

        Two shapes are observed:
          * [ctrl_idx, [1, [attr_idx]]]              — plain field binding
          * [ctrl_idx, [2, [attr_idx], [0, uuid]]]   — table/composite binding
        """
        for entry in section[1:]:
            if not (isinstance(entry, list) and len(entry) == 2
                    and isinstance(entry[0], int) and isinstance(entry[1], list)
                    and entry[1]):
                continue
            kind = entry[1][0]
            attr_idx = -1
            if kind == 1:
                if (len(entry[1]) > 1 and isinstance(entry[1][1], list)
                        and entry[1][1] and isinstance(entry[1][1][0], int)):
                    attr_idx = entry[1][1][0]
            elif kind == 2:
                if (len(entry[1]) > 1 and isinstance(entry[1][1], list)
                        and entry[1][1] and isinstance(entry[1][1][0], int)):
                    attr_idx = entry[1][1][0]
            if attr_idx >= 0:
                result.data_bindings.append(FormDataBinding(
                    control_index=entry[0],
                    attribute_index=attr_idx,
                ))

    def _looks_like_attribute_section(self, item: Any) -> bool:
        """Detect an attributes wrapper {1|3|4|16, entry1, entry2, ...}."""
        if not isinstance(item, list) or len(item) < 2:
            return False
        if item[0] not in (1, 3, 4, 16):
            return False
        # Wrapper 16 is a {16, 1, ...} layout block when item[1] is an int;
        # the attribute form has a list of entries as item[1].
        if item[0] == 16 and isinstance(item[1], int):
            return False
        for entry in item[1:]:
            if not isinstance(entry, list):
                continue
            has_string = any(isinstance(x, str) for x in entry)
            if not has_string:
                continue
            has_pattern = any(
                isinstance(x, list) and x and x[0] == "Pattern" for x in entry
            )
            # Known layouts: [[idx],f,d,"Name",{Pattern}] (type 3)
            # or [{idx},f,d,?, "Name",{Pattern}] (type 1 wrapper)
            if has_pattern:
                return True
            if (len(entry) >= 4 and isinstance(entry[0], list)
                    and isinstance(entry[3], str)):
                return True
        return False

    def _recursive_find_attributes(self, obj: Any, result: ParsedForm):
        """Recursively search for attribute sections."""
        if not isinstance(obj, list):
            return
        if self._is_attribute_section(obj):
            self._parse_attribute_section(obj, result)
            return
        for item in obj:
            if isinstance(item, list):
                self._recursive_find_attributes(item, result)

    def _is_attribute_section(self, item: Any) -> bool:
        """Check if an item looks like a form attribute section."""
        if not isinstance(item, list) or len(item) < 3:
            return False
        # Structure: [3, entry1, entry2, ...] where each entry is
        # [[index], flags, direction, "Name", {"Pattern",...}]
        if item[0] != 3:
            return False
        # Check second element is an attribute entry
        second = item[1]
        if (isinstance(second, list) and len(second) >= 4
                and isinstance(second[0], list) and isinstance(second[3], str)):
            return True
        return False

    def _find_events_in_root(self, root: list, result: ParsedForm):
        """Find form-level events in root elements."""
        for item in root:
            if not isinstance(item, list) or len(item) < 2:
                continue
            # Events section: {count, {event_type, uuid, ...}, ...}
            if (isinstance(item[0], int) and item[0] > 0
                    and isinstance(item[1], list) and item[1]
                    and isinstance(item[1][0], int)
                    and item[1][0] in EVENT_TYPE_MAP):
                count = item[0]
                for j in range(1, min(count + 1, len(item))):
                    evt_block = item[j]
                    if isinstance(evt_block, list):
                        evt = self._parse_event_block(evt_block)
                        if evt:
                            evt.control_index = -1  # form-level
                            result.events.append(evt)

    def _parse_columns(self, obj: Any) -> List[FormColumn]:
        """Parse table columns from a {8, col1, col2, ...} block.

        Each column is [uuid, [1, [8, [19, [1,1,{"ru",title}], ..., "Name", ...,
        ["Pattern",[...]]]]]]. The column name is a plain string inside the {19}
        block; the title is the localized pair in the same block.
        """
        columns: List[FormColumn] = []
        if not isinstance(obj, list):
            return columns
        if obj and obj[0] == 8 and len(obj) > 1:
            for entry in obj[1:]:
                col = self._parse_column_entry(entry)
                if col:
                    columns.append(col)
            return columns
        for item in obj:
            if isinstance(item, list):
                found = self._parse_columns(item)
                if found:
                    return found
        return columns

    def _parse_column_entry(self, entry: Any) -> Optional[FormColumn]:
        """Parse one column entry [uuid, [1, [8, [19, title, ..., "Name", pattern]]]]."""
        if not (isinstance(entry, list) and len(entry) >= 2
                and isinstance(entry[0], str) and _is_uuid(entry[0])):
            return None
        node = entry[1]
        if not isinstance(node, list):
            return None
        data = _find_nested_block(node, 19)
        if data is None:
            return None
        col = FormColumn(uuid=entry[0])
        # title: {1,1,{"ru",...}} inside the {19} block
        for it in data:
            if _is_loc_pair(it):
                col.title = it[2][1]
                break
        # name: the plain string immediately before the empty list "[]"
        for i in range(1, len(data)):
            if (isinstance(data[i], list) and not data[i]
                    and isinstance(data[i - 1], str)
                    and data[i - 1] not in ("ru", "")):
                col.name = data[i - 1]
                break
        # type from the pattern block
        pattern = None
        for it in data:
            if isinstance(it, list) and it and it[0] == "Pattern" and len(it) >= 2:
                pattern = it
                break
        if pattern is not None and isinstance(pattern[1], list) and pattern[1]:
            sub = pattern[1]
            if sub[0] == "#":
                col.type_pattern = "#"
                col.type_uuid = sub[1] if len(sub) > 1 else ""
            else:
                col.type_pattern = sub[0]
                col.type_1c, col.type_xml = resolve_attribute_type(sub[0])
                if sub[0] == "S" and len(sub) > 1 and isinstance(sub[1], int):
                    col.type_length = sub[1]
        return col

    def _collect_element_events(self, obj: Any) -> List[FormEvent]:
        """Collect element-level event handlers from {21,...} blocks.

        Shape: {21, [event_id, handler_uuid, {3,"Method",...}], ...}. Each entry
        binds one event (event_id) to a handler method on the control.
        """
        events: List[FormEvent] = []
        if not isinstance(obj, list):
            return events
        if obj and obj[0] == 21:
            for entry in obj[1:]:
                if not (isinstance(entry, list) and len(entry) >= 3
                        and isinstance(entry[0], int)):
                    continue
                evt = FormEvent()
                evt.event_type_id = entry[0]
                ru, en = resolve_element_event(entry[0])
                evt.event_name_ru = ru
                evt.event_name_en = en
                for sub in entry[2:]:
                    if (isinstance(sub, list) and sub and sub[0] == 3
                            and len(sub) > 1 and isinstance(sub[1], str)):
                        evt.handler_method = sub[1]
                        break
                if evt.handler_method:
                    events.append(evt)
            return events
        for item in obj:
            if isinstance(item, list):
                events.extend(self._collect_element_events(item))
        return events

    def _collect_commands(self, obj: Any) -> List[FormCommand]:
        """Recursively collect command definitions from a control's raw blocks.

        A command appears in one of two shapes:
          * {7, uuid, ..., {3,"Action",...}}  — command-bar command
          * {3,"Name",{1,"Name",{1,1,{"ru",title}},...}} — button action
        """
        commands: List[FormCommand] = []
        if not isinstance(obj, list):
            return commands
        # {21,...} is an element-event-handler block, not commands.
        if obj and obj[0] == 21:
            return commands
        if obj and obj[0] == 7 and len(obj) > 1:
            cmd = self._parse_command_block(obj)
            if cmd:
                commands.append(cmd)
        elif obj and obj[0] == 3 and len(obj) > 1 and isinstance(obj[1], str):
            # {3,"Name",{1,"Name",...}} — a command definition (button action).
            if self._is_command_definition(obj):
                cmd = self._parse_command_block(obj)
                if cmd:
                    commands.append(cmd)
        for item in obj:
            if isinstance(item, list):
                commands.extend(self._collect_commands(item))
        return commands

    def _is_command_definition(self, block: list) -> bool:
        """True if a {3,"Name",...} block is a command definition.

        A command definition carries a nested {1,"Name",...} whose value equals
        the {3,"Name",...} action name. A plain type-data {3,...} block does not.
        """
        if len(block) < 2 or not isinstance(block[1], str):
            return False
        name = block[1]
        for item in block[2:]:
            if (isinstance(item, list) and len(item) >= 2
                    and item[0] == 1 and item[1] == name):
                return True
        return False

    def _parse_command_block(self, block: list) -> Optional[FormCommand]:
        """Parse a command block into a FormCommand.

        Accepts both {7, uuid, ..., {3,"Action",...}} and
        {3,"Action",{1,"Action",{1,1,{"ru",title}},...}} shapes.
        """
        if len(block) < 2:
            return None
        cmd = FormCommand()
        # Action name: {3,"Name",...} has it at block[1]; {7,...,{3,"Name",...}}
        # has it in a nested {3,...} block.
        if block[0] == 3 and isinstance(block[1], str):
            cmd.action = block[1]
            cmd.name = block[1]
        else:
            for item in block[1:]:
                if isinstance(item, list) and item and item[0] == 3:
                    if len(item) > 1 and isinstance(item[1], str):
                        cmd.action = item[1]
                        cmd.name = item[1]
                        break
        # Display title from a localized block nested in the command definition
        title = _find_command_title(block)
        if title and title != "ru":
            cmd.title = title
        if not cmd.name:
            return None
        return cmd

    def _parse_control_block(self, block: list) -> Optional[FormControl]:
        """Parse a control definition block {uuid, index, type_data, coords, cmd, ...}."""
        if len(block) < 2 or not _is_uuid(block[0]):
            return None

        ctrl = FormControl()
        ctrl.uuid = block[0]
        ctrl.index = block[1] if isinstance(block[1], int) else 0
        ctrl.control_type_uuid = block[0]
        ctrl.xml_tag = resolve_control_type(block[0]) or "UnknownControl"

        # Scan the rest of the block for sub-structures
        for item in block[2:]:
            if not isinstance(item, list):
                continue

            if not item:
                continue

            # Coordinate block {8,L,T,R,B,...}
            if item[0] == 8 and len(item) >= 5:
                l, t, r, b = item[1], item[2], item[3], item[4]
                ctrl.x = int(l)
                ctrl.y = int(t)
                ctrl.width = int(r) - int(l)
                ctrl.height = int(b) - int(t)

            # Element name {14,"Name",...} — the real element name, not a command.
            elif item[0] == 14 and len(item) >= 2 and isinstance(item[1], str):
                ctrl.element_name = item[1]

            # Type pattern + data binding {9,["Pattern",[...]], ...}
            elif item[0] == 9 and len(item) > 1 and isinstance(item[1], list):
                self._parse_type_binding(item, ctrl)

            # Sub-control definitions (nested UUID blocks)
            elif isinstance(item[0], str) and _is_uuid(item[0]) and not is_system_uuid(item[0]):
                sub = self._parse_control_block(item)
                if sub:
                    ctrl.raw_blocks.append(sub)

        # Find the control name and display title
        self._find_control_name(block, ctrl)

        # Element-level event handlers {21, [event_id, uuid, {3,"Method",...}], ...}
        ctrl.events = self._collect_element_events(block)

        # Table columns (value table / spreadsheet): {8, col1, col2, ...}
        if ctrl.xml_tag in ("Table", "SpreadSheetDocumentField"):
            ctrl.columns = self._parse_columns(block)

        # A Button's command is the action defined in its own block.
        if ctrl.xml_tag == 'Button':
            for cmd in self._collect_commands(block):
                if cmd.action:
                    ctrl.command_name = cmd.action
                    break

        # Choice list (e.g. InputField with a drop-down list of values).
        ctrl.choice_list = self._parse_choice_list(block)

        return ctrl

    def _parse_choice_list(self, block: list) -> List[Dict[str, str]]:
        """Extract a choice list from a control's {9,...} block.

        The choice list lives in a {9, [2, cols...], [2,2,0,0,1,1,[1,2,item...]], ...}
        block. Each item is [2,0,2,['S',value],['#',uuid,[1,'ru',title]],0].
        Returns [{value, title}, ...].
        """
        def find9(x: Any) -> Optional[list]:
            if not isinstance(x, list):
                return None
            if x and x[0] == 9:
                return x
            for it in x:
                r = find9(it)
                if r:
                    return r
            return None

        f9 = find9(block)
        if not f9 or len(f9) < 3:
            return []
        # Items are in f9[2] = [2,2,0,0,1,1,[1,2,item...],-1,1]
        items_wrapper = f9[2]
        if not isinstance(items_wrapper, list) or len(items_wrapper) < 7:
            return []
        rows = items_wrapper[6]
        if not isinstance(rows, list) or len(rows) < 2:
            return []
        items = rows[2:]  # rows = [1, 2, item1, item2, ...]
        if not isinstance(items, list):
            return []

        result: List[Dict[str, str]] = []
        for item in items:
            if not isinstance(item, list) or len(item) < 5:
                continue
            value = ''
            title = ''
            # value at item[3] = ['S', value]
            if isinstance(item[3], list) and len(item[3]) > 1:
                value = str(item[3][1])
            # title at item[4] = ['#', uuid, [1,'ru',title]]
            if isinstance(item[4], list) and len(item[4]) > 2:
                loc = item[4][2]
                if isinstance(loc, list) and len(loc) > 2 and loc[0] == 1:
                    title = str(loc[2])
            result.append({"value": value, "title": title})
        return result

    def _parse_type_binding(self, item: list, ctrl: FormControl):
        """Parse BLK[9]: {9,["Pattern",["S",len,min]|["#",uuid]|"D"|...], props...}."""
        # Type pattern at item[1][0]
        pattern_block = item[1][0] if item[1] and isinstance(item[1][0], list) else None
        if isinstance(pattern_block, list) and pattern_block and pattern_block[0] == "Pattern":
            if len(pattern_block) > 1 and isinstance(pattern_block[1], list) and pattern_block[1]:
                sub = pattern_block[1]
                if sub[0] == "#":
                    ctrl.type_pattern = "#"
                    ctrl.type_uuid = sub[1] if len(sub) > 1 and isinstance(sub[1], str) else ""
                elif isinstance(sub[0], str):
                    ctrl.type_pattern = sub[0]
                    ctrl.type_1c, ctrl.type_xml = resolve_attribute_type(sub[0])
                    if sub[0] == "S" and len(sub) > 1 and isinstance(sub[1], int):
                        ctrl.type_length = sub[1]

        # Attribute reference: [1,[uuid,[4,{"U"},...]]] inside item[1]
        for node in item[1]:
            if (isinstance(node, list) and len(node) >= 2 and isinstance(node[0], int)
                    and node[0] == 1 and isinstance(node[1], list) and node[1]
                    and _is_uuid(node[1][0])):
                ctrl.data_binding_uuid = node[1][0]
                break

    def _find_control_name(self, block: list, ctrl: FormControl):
        """Find an element name, display title, and tooltip for a control.

        The element name comes from {14,"Name",...} (already captured in
        ``ctrl.element_name``). The display title is a localized string that is
        a sibling of the control's ``[16,...]`` layout marker; the tooltip /
        comment is a localized string nested inside that marker block.
        """
        # Element name from {14,"Name",...}
        if ctrl.element_name:
            ctrl.name = ctrl.element_name

        title, tooltip = _extract_title_tooltip(block)
        if title and title != "ru" and not title.startswith("Страница"):
            ctrl.title = title
        if tooltip and tooltip != "ru":
            ctrl.tooltip = tooltip

        # Fallback name from the title if no element name was found.
        if not ctrl.name and ctrl.title:
            ctrl.name = ctrl.title

    def _parse_attribute_section(self, section: list, result: ParsedForm):
        """Parse a form attribute section.

        Supports both observed layouts:
          type-3 wrapper:  {3, [[idx],f,d,"Name",{Pattern}], ...}
          type-1 wrapper:  {1, [{idx},f,d,f2,"Name",{Pattern}], ...}
        """
        for entry in section[1:]:  # skip the leading marker
            if not isinstance(entry, list) or not entry:
                continue
            attr = FormAttribute()
            if isinstance(entry[0], list) and entry[0] and isinstance(entry[0][0], int):
                attr.index = entry[0][0]

            # Robust field extraction: name is the first string, pattern the first
            # "Pattern" block regardless of exact position.
            for pos, val in enumerate(entry):
                if pos == 0 and isinstance(val, list) and val:
                    continue  # index block
                if isinstance(val, str) and not attr.name:
                    attr.name = val
                elif isinstance(val, list) and val and val[0] == "Pattern" and not attr.type_pattern:
                    self._apply_attribute_pattern(val, attr)
                elif isinstance(val, int) and not attr.flags:
                    attr.flags = val
                elif isinstance(val, int) and not attr.direction:
                    attr.direction = val

            if attr.name:
                result.attributes.append(attr)

    def _apply_attribute_pattern(self, pattern_block: list, attr: FormAttribute):
        """Parse a "Pattern" type block into an attribute's type fields."""
        if len(pattern_block) > 1 and isinstance(pattern_block[1], list) and pattern_block[1]:
            type_sub = pattern_block[1]
            if type_sub[0] == "#":
                attr.type_pattern = "#"
                attr.type_uuid = type_sub[1] if len(type_sub) > 1 else ""
            else:
                attr.type_pattern = type_sub[0]
                if type_sub[0] == "S" and len(type_sub) > 1:
                    attr.type_1c = f"Строка({type_sub[1]})"
                    attr.type_length = int(type_sub[1]) if isinstance(type_sub[1], int) else 0
                else:
                    attr.type_1c, attr.type_xml = resolve_attribute_type(type_sub[0])
        else:
            attr.type_pattern = "Pattern"
            attr.type_1c = "Произвольный"

    def _detect_main_attribute(self, result: ParsedForm):
        """Detect and flag the main (object) attribute of the form."""
        for attr in result.attributes:
            # Document/Catalog/DataProcessor object attributes follow the
            # "<ObjectType>Объект" convention, reports use "<Type>Отчет" etc.
            if attr.name.endswith("Объект") or attr.name == "Отчет" or attr.name == "Документ":
                result.main_attribute = attr.name
                return
        if result.attributes:
            result.main_attribute = result.attributes[0].name

    def _parse_event_block(self, block: list) -> Optional[FormEvent]:
        """Parse an event binding block {event_type, uuid, {3, method, ...}}."""
        if len(block) < 2:
            return None

        event_type_id = block[0] if isinstance(block[0], int) else 0
        if event_type_id not in EVENT_TYPE_MAP and event_type_id != 2147483647:
            return None

        evt = FormEvent()
        evt.event_type_id = event_type_id
        name_pair = EVENT_TYPE_MAP.get(event_type_id)
        if name_pair:
            evt.event_name_ru = name_pair[0]
            evt.event_name_en = name_pair[1]

        # Find method name in {3,"MethodName",...} block
        for item in block[1:]:
            if isinstance(item, list) and item and item[0] == 3:
                if len(item) > 1 and isinstance(item[1], str):
                    evt.handler_method = item[1]
                    break

        return evt

    # ------------------------------------------------------------------
    # High-level API: parse text and return enriched ParsedForm
    # ------------------------------------------------------------------

    def parse_enriched(self, text: str, module_code: str = "") -> ParsedForm:
        """
        Parse form structure text and enrich with control names,
        event bindings, and data paths.
        """
        result = self.parse(text)

        # Enrich controls with events (scan for element-level events)
        self._enrichControlEvents(text, result)

        # Enrich controls with data paths from bindings
        self._enrichDataPaths(result)

        # Assign default names to unnamed controls
        self._assign_default_names(result)

        # Build the synthetic control tree
        result.tree = self.build_tree(result)

        return result

    def _enrichControlEvents(self, text: str, result: ParsedForm):
        """Find element-level events in the text and attach to controls."""
        # Element-level events: {2147483647, uuid, {3, "MethodName", ...}}
        # The UUID identifies which control gets the event
        # We need to match UUIDs to control indices

        # Build UUID→index map
        uuid_to_index: Dict[str, int] = {}
        for ctrl in result.controls:
            if ctrl.uuid:
                uuid_to_index[ctrl.uuid] = ctrl.index

        # Find all event blocks
        for m in re.finditer(
            r'\{(70000|70001|70002|70003|70009|80000|2147483647),'
            r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
            text,
        ):
            event_type_id = int(m.group(1))
            uuid = m.group(2)

            # Find the method name in {3,"MethodName",...}
            rest = text[m.end():m.end() + 500]
            method_match = re.search(r'\{3,"([^"]*)"', rest)
            if not method_match:
                continue

            evt = FormEvent()
            evt.event_type_id = event_type_id
            evt.handler_method = method_match.group(1)

            name_pair = EVENT_TYPE_MAP.get(event_type_id)
            if name_pair:
                evt.event_name_ru = name_pair[0]
                evt.event_name_en = name_pair[1]

            # Attach to control
            ctrl_idx = uuid_to_index.get(uuid, -1)
            evt.control_index = ctrl_idx

            if ctrl_idx >= 0:
                for ctrl in result.controls:
                    if ctrl.index == ctrl_idx:
                        ctrl.events.append(evt)
                        break
            else:
                result.events.append(evt)

    def _enrichDataPaths(self, result: ParsedForm):
        """Map data bindings to controls.

        Resolution order:
          1. Legacy data_bindings (control_index -> attribute_index)
          2. Control element name matches a form attribute name
          3. Managed forms with a main attribute: field inputs bind to
             "<MainAttribute>.<ElementName>" (e.g. ДокументОбъект.Номер).
        """
        # 1. Direct binding match by index
        for binding in result.data_bindings:
            for ctrl in result.controls:
                if ctrl.index == binding.control_index:
                    ctrl.data_attribute_index = binding.attribute_index
                    for attr in result.attributes:
                        if attr.index == binding.attribute_index:
                            ctrl.data_path = attr.name
                            break
                    break

        attrs_by_name = {a.name: a for a in result.attributes}
        has_bindings = bool(result.data_bindings)

        for ctrl in result.controls:
            if ctrl.data_path:
                continue
            # 2. Element name matches a form attribute
            if ctrl.element_name and ctrl.element_name in attrs_by_name:
                ctrl.data_path = ctrl.element_name
                continue
            # 3. Field controls under the main attribute. Only as a fallback
            # when the form carries no explicit bindings; otherwise it would
            # fabricate paths for unbound controls.
            if has_bindings:
                continue
            if result.main_attribute and ctrl.xml_tag in (
                "InputField", "CheckBoxField", "CalendarField",
                "PictureField", "Table",
            ):
                if ctrl.element_name and not ctrl.element_name.startswith("Надпись"):
                    ctrl.data_path = f"{result.main_attribute}.{ctrl.element_name}"

    def _assign_default_names(self, result: ParsedForm):
        """Assign default names to unnamed controls."""
        type_counts: Dict[str, int] = {}
        for ctrl in result.controls:
            if not ctrl.name:
                tag = ctrl.xml_tag or "Control"
                type_counts[tag] = type_counts.get(tag, 0) + 1
                ctrl.name = f"{tag}{type_counts[tag]}"

    # ------------------------------------------------------------------
    # Control tree construction (synthetic grid)
    # ------------------------------------------------------------------

    GROUP_TAGS = {
        "UsualGroup",
        "Pages",
        "CommandBar",
        "ButtonGroup",
    }

    FIELD_TAGS = {
        "InputField",
        "CheckBoxField",
        "RadioButtonField",
        "CalendarField",
        "PictureField",
        "TextDocumentField",
        "SpreadSheetDocumentField",
        "Table",
    }

    def _bbox(self, ctrl: FormControl):
        """Return (left, top, right, bottom)."""
        return (ctrl.x, ctrl.y, ctrl.x + ctrl.width, ctrl.y + ctrl.height)

    def _contains(self, outer: FormControl, inner: FormControl) -> bool:
        """True if the inner control lies within the outer container's bounds."""
        ol, ot, or_, ob = self._bbox(outer)
        il, it, ir_, ib = self._bbox(inner)
        return (ol <= il and ot <= it and or_ >= ir_ and ob >= ib)

    def build_tree(self, result: ParsedForm) -> Optional[FormTreeNode]:
        """Build a synthetic control tree from bounding-box containment.

        Each control is placed inside the smallest container
        (UsualGroup/Pages/CommandBar/ButtonGroup) whose bounds contain it;
        otherwise it is attached to the root. Containers are nested the same
        way. Produces each control exactly once.
        """
        if not result.controls:
            return None

        root = FormTreeNode(index=-1, name="__root__", kind="group")
        containers = [c for c in result.controls if c.xml_tag in self.GROUP_TAGS]

        # Parent assignment: for each control pick the smallest containing
        # container (excluding itself); -1 means the root.
        parent_of: Dict[int, int] = {}
        for ctrl in result.controls:
            best = -1
            best_area = float("inf")
            for cont in containers:
                if cont.index == ctrl.index:
                    continue
                if self._contains(cont, ctrl):
                    area = cont.width * cont.height or 1
                    if area < best_area:
                        best = cont.index
                        best_area = area
            parent_of[ctrl.index] = best

        ctrl_by_index = {c.index: c for c in result.controls}
        nodes: Dict[int, FormTreeNode] = {}
        for ctrl in result.controls:
            kind = "group" if ctrl.xml_tag in self.GROUP_TAGS else "leaf"
            nodes[ctrl.index] = FormTreeNode(
                index=ctrl.index, name=ctrl.name, ctrl=ctrl, kind=kind
            )
        for ctrl in result.controls:
            parent_index = parent_of[ctrl.index]
            parent_node = root if parent_index == -1 else nodes[parent_index]
            parent_node.children.append(nodes[ctrl.index])

        self._sort_children(root)
        return root

    def _sort_children(self, node: FormTreeNode):
        """Sort child nodes by (y, x) then index for deterministic output."""
        def key(n: FormTreeNode):
            c = n.ctrl
            return (c.y if c else 0, c.x if c else 0, n.index)
        node.children.sort(key=key)
        for child in node.children:
            if child.kind == "group":
                self._sort_children(child)
