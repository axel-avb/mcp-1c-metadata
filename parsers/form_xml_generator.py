"""
Generator for 1C managed form XML (Form.xml) from ParsedForm AST.

Produces a spec-compliant Form.xml (version 2.17, default ``logform``
namespace, 17 namespace declarations, element order per the managed-forms
spec) that can be loaded by the 1C:Enterprise Configurator.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

from parsers.form_structure_parser import (
    FormAttribute,
    FormCommand,
    FormControl,
    FormEvent,
    FormTreeNode,
    ParsedForm,
)
from parsers.form_type_mapper import (
    ELEMENT_EVENT_PATTERNS,
    FORM_EVENT_XML_NAMES,
    resolve_type_uuid,
)

# ---------------------------------------------------------------------------
# Root element and namespaces (spec §1) — all 17 declarations are identical
# across every form in a configuration.
# ---------------------------------------------------------------------------

_NAMESPACES: List[Tuple[str, str]] = [
    ("xmlns", "http://v8.1c.ru/8.3/xcf/logform"),
    ("xmlns:app", "http://v8.1c.ru/8.2/managed-application/core"),
    ("xmlns:cfg", "http://v8.1c.ru/8.1/data/enterprise/current-config"),
    ("xmlns:dcscor", "http://v8.1c.ru/8.1/data-composition-system/core"),
    ("xmlns:dcssch", "http://v8.1c.ru/8.1/data-composition-system/schema"),
    ("xmlns:dcsset", "http://v8.1c.ru/8.1/data-composition-system/settings"),
    ("xmlns:ent", "http://v8.1c.ru/8.1/data/enterprise"),
    ("xmlns:lf", "http://v8.1c.ru/8.2/managed-application/logform"),
    ("xmlns:style", "http://v8.1c.ru/8.1/data/ui/style"),
    ("xmlns:sys", "http://v8.1c.ru/8.1/data/ui/fonts/system"),
    ("xmlns:v8", "http://v8.1c.ru/8.1/data/core"),
    ("xmlns:v8ui", "http://v8.1c.ru/8.1/data/ui"),
    ("xmlns:web", "http://v8.1c.ru/8.1/data/ui/colors/web"),
    ("xmlns:win", "http://v8.1c.ru/8.1/data/ui/colors/windows"),
    ("xmlns:xr", "http://v8.1c.ru/8.3/xcf/readable"),
    ("xmlns:xs", "http://www.w3.org/2001/XMLSchema"),
    ("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance"),
]

_VERSION = "2.17"

# Elements that carry <DataPath> (data-bound controls).
_DATAPATH_TAGS = {
    "InputField",
    "LabelField",
    "CheckBoxField",
    "RadioButtonField",
    "CalendarField",
    "PictureField",
    "Table",
}


def _multilang(text: str, indent: int) -> List[str]:
    """Render a multilang value (spec §7.3)."""
    pad = " " * indent
    return [
        f'{pad}  <v8:item>',
        f'{pad}    <v8:lang>ru</v8:lang>',
        f'{pad}    <v8:content>{escape(text)}</v8:content>',
        f'{pad}  </v8:item>',
    ]


class FormXmlGenerator:
    """
    Generates Form.xml (managed form) from a ParsedForm AST.

    Usage:
        generator = FormXmlGenerator()
        xml_text = generator.generate(parsed_form)
        generator.generate_to_file(parsed_form, output_path)
    """

    def __init__(self):
        self._id_counter = 0
        self._name_ids: Dict[str, int] = {}

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _id_for(self, key: str) -> int:
        if key not in self._name_ids:
            self._name_ids[key] = self._next_id()
        return self._name_ids[key]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, form: ParsedForm, module_code: str = "") -> str:
        """Generate Form.xml content from ParsedForm."""
        self._id_counter = 0
        self._name_ids = {}

        lines: List[str] = []
        lines.append('<?xml version="1.0" encoding="UTF-8"?>')
        ns_decl = " ".join(f'{k}="{v}"' for k, v in _NAMESPACES)
        lines.append(f'<Form {ns_decl} version="{_VERSION}">')

        # Section order per spec §2:
        # properties → CommandSet → AutoCommandBar → Events →
        # ChildItems → Attributes → Parameters → Commands
        self._gen_form_properties(form, lines)
        self._gen_command_set(lines)
        self._gen_auto_command_bar(lines)
        self._gen_form_events(form, lines)
        self._gen_child_items(form, lines)
        self._gen_attributes(form, lines)
        self._gen_parameters(lines)
        self._gen_commands(form, lines)

        lines.append('</Form>')
        return '\n'.join(lines)

    def generate_to_file(self, form: ParsedForm, output_path, module_code: str = ""):
        """Generate and write Form.xml to file."""
        from pathlib import Path
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.generate(form, module_code), encoding='utf-8')

    # ------------------------------------------------------------------
    # Form properties (spec §3)
    # ------------------------------------------------------------------

    def _gen_form_properties(self, form: ParsedForm, lines: List[str]):
        if form.title:
            lines.append('  <Title>')
            lines.extend(_multilang(form.title, 2))
            lines.append('  </Title>')
        if form.width:
            lines.append(f'  <Width>{form.width}</Width>')
        if form.height:
            lines.append(f'  <Height>{form.height}</Height>')
        lines.append('  <WindowOpeningMode>Modeless</WindowOpeningMode>')
        lines.append('  <AutoTitle>false</AutoTitle>')
        lines.append('  <CommandBarLocation>Top</CommandBarLocation>')

    # ------------------------------------------------------------------
    # CommandSet / AutoCommandBar (spec §4, §5)
    # ------------------------------------------------------------------

    def _gen_command_set(self, lines: List[str]):
        """Excluded standard commands (none currently known)."""
        lines.append('  <CommandSet/>')

    def _gen_auto_command_bar(self, lines: List[str]):
        """Main command bar — always present with fixed name/id."""
        lines.append('  <AutoCommandBar name="ФормаКоманднаяПанель" id="-1" />')

    # ------------------------------------------------------------------
    # Form events (spec §6)
    # ------------------------------------------------------------------

    def _form_event_name(self, evt: FormEvent) -> Optional[str]:
        if evt.event_type_id == 2147483647:
            # Element-level handler — try to infer a concrete event name
            return self._element_event_name(evt.handler_method)
        return FORM_EVENT_XML_NAMES.get(evt.event_type_id) or evt.event_name_en or None

    def _element_event_name(self, handler_method: str) -> Optional[str]:
        for ru, en in ELEMENT_EVENT_PATTERNS.items():
            if ru and ru in handler_method:
                return en
        return None

    def _gen_form_events(self, form: ParsedForm, lines: List[str]):
        events = [e for e in form.events if e.control_index == -1]
        rendered = []
        seen = set()
        for e in events:
            if not e.handler_method:
                continue
            name = self._form_event_name(e)
            if not name:
                continue
            key = (name, e.handler_method)
            if key in seen:
                continue
            seen.add(key)
            rendered.append((name, e.handler_method))
        if not rendered:
            return
        lines.append('  <Events>')
        for name, method in rendered:
            lines.append(f'    <Event name="{name}">{escape(method)}</Event>')
        lines.append('  </Events>')

    # ------------------------------------------------------------------
    # ChildItems — control tree (spec §7, §8)
    # ------------------------------------------------------------------

    def _gen_child_items(self, form: ParsedForm, lines: List[str]):
        if not form.tree or not form.tree.children:
            return
        lines.append('  <ChildItems>')
        for node in form.tree.children:
            self._gen_node(node, lines, indent=2)
        lines.append('  </ChildItems>')

    def _gen_node(self, node: FormTreeNode, lines: List[str], indent: int):
        if node.kind == 'group':
            self._gen_group(node, lines, indent)
        else:
            self._gen_leaf(node, lines, indent)

    def _gen_group(self, node: FormTreeNode, lines: List[str], indent: int):
        pad = ' ' * indent
        ctrl = node.ctrl
        name = ctrl.name if ctrl and ctrl.name else f"Группа{node.index}"
        gid = self._id_for(f"grp_{node.index}")
        lines.append(f'{pad}<UsualGroup name="{escape(name)}" id="{gid}">')
        lines.append(f'{pad}  <Group>Vertical</Group>')
        if ctrl and ctrl.title:
            lines.append(f'{pad}  <Title>')
            lines.extend(_multilang(ctrl.title, indent + 2))
            lines.append(f'{pad}  </Title>')
        if node.children:
            lines.append(f'{pad}  <ChildItems>')
            for child in node.children:
                self._gen_node(child, lines, indent + 2)
            lines.append(f'{pad}  </ChildItems>')
        lines.append(f'{pad}</UsualGroup>')

    def _gen_leaf(self, node: FormTreeNode, lines: List[str], indent: int):
        pad = ' ' * indent
        ctrl = node.ctrl
        if ctrl is None or not ctrl.name:
            return
        tag = ctrl.xml_tag or 'UnknownControl'
        if tag == 'UnknownControl':
            logger.warning(
                "Unknown control type for element %r (uuid=%s); "
                "falling back to LabelField",
                ctrl.name, ctrl.uuid,
            )
            tag = 'LabelField'
        eid = self._id_for(f"el_{ctrl.index}")
        lines.append(f'{pad}<{tag} name="{escape(ctrl.name)}" id="{eid}">')

        if ctrl.title:
            lines.append(f'{pad}  <Title>')
            lines.extend(_multilang(ctrl.title, indent + 2))
            lines.append(f'{pad}  </Title>')

        if ctrl.tooltip:
            lines.append(f'{pad}  <ToolTip>')
            lines.extend(_multilang(ctrl.tooltip, indent + 2))
            lines.append(f'{pad}  </ToolTip>')

        if tag in _DATAPATH_TAGS and ctrl.data_path:
            lines.append(f'{pad}  <DataPath>{escape(ctrl.data_path)}</DataPath>')

        if tag == 'Button' and ctrl.command_name:
            lines.append(f'{pad}  <CommandName>{escape(ctrl.command_name)}</CommandName>')

        events = [e for e in ctrl.events if e.handler_method]
        if events:
            rendered = []
            seen = set()
            for e in events:
                name = self._form_event_name(e)
                if not name:
                    continue
                key = (name, e.handler_method)
                if key in seen:
                    continue
                seen.add(key)
                rendered.append((name, e.handler_method))
            if rendered:
                lines.append(f'{pad}  <Events>')
                for name, method in rendered:
                    lines.append(
                        f'{pad}    <Event name="{name}">{escape(method)}</Event>'
                    )
                lines.append(f'{pad}  </Events>')

        lines.append(f'{pad}</{tag}>')

    # ------------------------------------------------------------------
    # Attributes (spec §9)
    # ------------------------------------------------------------------

    def _gen_attributes(self, form: ParsedForm, lines: List[str]):
        if not form.attributes:
            return
        lines.append('  <Attributes>')
        for attr in form.attributes:
            aid = self._id_for(f"attr_{attr.name}")
            lines.append(f'    <Attribute name="{escape(attr.name)}" id="{aid}">')
            if attr.name == form.main_attribute:
                lines.append('      <MainAttribute>true</MainAttribute>')
            lines.extend(self._gen_type(attr, 6))
            lines.append('    </Attribute>')
        lines.append('  </Attributes>')

    def _gen_type(self, attr: FormAttribute, indent: int) -> List[str]:
        """Render an attribute type block with qualifiers (spec §9.1)."""
        pad = ' ' * indent
        pattern = attr.type_pattern
        if pattern == '#' and attr.type_uuid:
            resolved = resolve_type_uuid(attr.type_uuid) or 'v8:Universal'
            return [
                f'{pad}<Type>',
                f'{pad}  <v8:Type>{resolved}</v8:Type>',
                f'{pad}</Type>',
            ]
        if pattern == 'S':
            length = attr.type_length or 100
            return [
                f'{pad}<Type>',
                f'{pad}  <v8:Type>xs:string</v8:Type>',
                f'{pad}  <v8:StringQualifiers>',
                f'{pad}    <v8:Length>{length}</v8:Length>',
                f'{pad}    <v8:AllowedLength>Variable</v8:AllowedLength>',
                f'{pad}  </v8:StringQualifiers>',
                f'{pad}</Type>',
            ]
        if pattern == 'N':
            return [
                f'{pad}<Type>',
                f'{pad}  <v8:Type>xs:decimal</v8:Type>',
                f'{pad}  <v8:NumberQualifiers>',
                f'{pad}    <v8:Digits>15</v8:Digits>',
                f'{pad}    <v8:FractionDigits>2</v8:FractionDigits>',
                f'{pad}    <v8:AllowedSign>Any</v8:AllowedSign>',
                f'{pad}  </v8:NumberQualifiers>',
                f'{pad}</Type>',
            ]
        if pattern == 'D':
            return [
                f'{pad}<Type>',
                f'{pad}  <v8:Type>xs:dateTime</v8:Type>',
                f'{pad}  <v8:DateQualifiers>',
                f'{pad}    <v8:DateFractions>DateTime</v8:DateFractions>',
                f'{pad}  </v8:DateQualifiers>',
                f'{pad}</Type>',
            ]
        if pattern == 'B':
            return [
                f'{pad}<Type>',
                f'{pad}  <v8:Type>xs:boolean</v8:Type>',
                f'{pad}</Type>',
            ]
        if pattern == 'U':
            return [
                f'{pad}<Type>',
                f'{pad}  <v8:Type>v8:UUID</v8:Type>',
                f'{pad}</Type>',
            ]
        return [f'{pad}<Type/>']

    # ------------------------------------------------------------------
    # Parameters (spec §10) — none currently extracted
    # ------------------------------------------------------------------

    def _gen_parameters(self, lines: List[str]):
        lines.append('  <Parameters/>')

    # ------------------------------------------------------------------
    # Commands (spec §11) — only real commands (ordinary forms)
    # ------------------------------------------------------------------

    def _collect_commands(self, form: ParsedForm) -> List[FormCommand]:
        """Collect unique form commands from the parsed AST."""
        return list(form.commands)

    def _gen_commands(self, form: ParsedForm, lines: List[str]):
        commands = self._collect_commands(form)
        if not commands:
            return
        lines.append('  <Commands>')
        for cmd in commands:
            cid = self._id_for(f"cmd_{cmd.name}")
            title = cmd.title or cmd.name
            lines.append(f'    <Command name="{escape(cmd.name)}" id="{cid}">')
            lines.append('      <Title>')
            lines.extend(_multilang(title, 6))
            lines.append('      </Title>')
            lines.append('      <ToolTip>')
            lines.extend(_multilang(title, 6))
            lines.append('      </ToolTip>')
            if cmd.action:
                lines.append(f'      <Action>{escape(cmd.action)}</Action>')
            lines.append('    </Command>')
        lines.append('  </Commands>')


def main():
    import argparse

    from parsers.form_bin_parser_v2 import V8FormBinParser
    from parsers.form_structure_parser import FormStructureParser

    cli = argparse.ArgumentParser(description='Генерация Form.xml из Form.bin.')
    cli.add_argument('input_path', help='исходный Form.bin')
    cli.add_argument('output_path', help='выходной Form.xml')
    args = cli.parse_args()

    bin_parser = V8FormBinParser()
    form_data = bin_parser.parse(Path(args.input_path))
    parsed_form = FormStructureParser().parse_enriched(form_data.form_structure_text)

    xml_text = FormXmlGenerator().generate(parsed_form, module_code=form_data.module_code)
    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(xml_text, encoding='utf-8')
    print(f'Generated {output_path} ({output_path.stat().st_size} bytes)')


if __name__ == '__main__':
    main()
