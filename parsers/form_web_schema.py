"""
Web JSON schema generator for 1C forms.

Produces a clean, website-rebuildable JSON schema from a ParsedForm AST.
The schema describes the form's structure (controls, hierarchy, attributes,
commands, events) in a framework-agnostic way so a website can render it.

The schema is derived from the same clean semantic AST that drives the
managed Form.xml generator — a single source of truth, two renderers.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from parsers.form_structure_parser import (
    FormAttribute,
    FormColumn,
    FormCommand,
    FormControl,
    FormEvent,
    FormTreeNode,
    ParsedForm,
)


# Control XML tag -> web widget type.
_WIDGET_MAP: Dict[str, str] = {
    "UsualGroup": "group",
    "InputField": "input",
    "CheckBoxField": "checkbox",
    "RadioButtonField": "radio",
    "CalendarField": "date",
    "PictureField": "image",
    "LabelField": "label",
    "Button": "button",
    "Hyperlink": "link",
    "Table": "table",
    "ProgressBarField": "progress",
    "Splitter": "splitter",
    "AutoCommandBar": "commandbar",
    "ColumnGroup": "group",
}


def _widget_type(tag: str) -> str:
    return _WIDGET_MAP.get(tag, "unknown")


def _type_schema(attr: FormAttribute) -> Dict[str, Any]:
    """Map an attribute's 1C type pattern to a JSON schema type."""
    pattern = attr.type_pattern
    if pattern == "S":
        return {"type": "string", "maxLength": attr.type_length or 0}
    if pattern == "B":
        return {"type": "boolean"}
    if pattern == "D":
        return {"type": "string", "format": "date-time"}
    if pattern == "N":
        return {"type": "number"}
    if pattern == "U":
        return {"type": "string", "format": "uuid"}
    if pattern == "#":
        return {"type": "object", "ref": attr.type_uuid or None}
    return {"type": "any"}


class FormWebSchemaGenerator:
    """
    Generates a website-rebuildable JSON schema from a ParsedForm AST.

    Usage:
        generator = FormWebSchemaGenerator()
        schema = generator.generate(parsed_form)
        generator.generate_to_file(parsed_form, output_path)
    """

    def generate(self, form: ParsedForm) -> Dict[str, Any]:
        """Generate the web JSON schema dict."""
        schema: Dict[str, Any] = {
            "schemaVersion": 1,
            "form": {
                "title": form.title,
                "width": form.width,
                "height": form.height,
                "type": form.form_type_name,
            },
            "attributes": [
                self._gen_attribute(a, form.main_attribute) for a in form.attributes
            ],
            "commands": [self._gen_command(c) for c in form.commands],
            "events": [self._gen_event(e) for e in form.events],
            "items": self._gen_items(form.tree) if form.tree else [],
        }
        return schema

    def generate_to_file(self, form: ParsedForm, output_path) -> None:
        """Generate and write the web JSON schema to file."""
        from pathlib import Path
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.generate(form), ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def _gen_attribute(self, attr: FormAttribute, main_attribute: str = "") -> Dict[str, Any]:
        return {
            "name": attr.name,
            "type": _type_schema(attr),
            "main": attr.name == main_attribute,
        }

    def _gen_command(self, cmd: FormCommand) -> Dict[str, Any]:
        return {
            "name": cmd.name,
            "title": cmd.title or cmd.name,
            "action": cmd.action,
        }

    def _gen_event(self, evt: FormEvent) -> Dict[str, Any]:
        return {
            "name": evt.event_name_en or evt.event_name_ru,
            "handler": evt.handler_method,
            "control": evt.control_index,
        }

    def _gen_items(self, node: Optional[FormTreeNode]) -> List[Dict[str, Any]]:
        if node is None:
            return []
        return [self._gen_node(child) for child in node.children]

    def _gen_node(self, node: FormTreeNode) -> Dict[str, Any]:
        ctrl = node.ctrl
        if ctrl is None:
            return {
                "kind": "group",
                "name": node.name or f"group_{node.index}",
                "children": [self._gen_node(c) for c in node.children],
            }
        item: Dict[str, Any] = {
            "name": ctrl.name,
            "kind": _widget_type(ctrl.xml_tag),
            "type": ctrl.xml_tag,
        }
        if ctrl.title:
            item["title"] = ctrl.title
        if ctrl.tooltip:
            item["tooltip"] = ctrl.tooltip
        if ctrl.data_path:
            item["dataPath"] = ctrl.data_path
        if ctrl.command_name:
            item["command"] = ctrl.command_name
        if ctrl.choice_list:
            item["choiceList"] = ctrl.choice_list
        if ctrl.columns:
            item["columns"] = [self._gen_column(c) for c in ctrl.columns]
        if ctrl.width or ctrl.height:
            item["size"] = {"width": ctrl.width, "height": ctrl.height}
        if ctrl.events:
            item["events"] = [self._gen_event(e) for e in ctrl.events]
        if node.children:
            item["children"] = [self._gen_node(c) for c in node.children]
        return item

    def _gen_column(self, col: FormColumn) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "name": col.name,
            "type": col.type_xml or col.type_1c or "any",
        }
        if col.title:
            item["title"] = col.title
        return item


def main():
    import argparse
    from pathlib import Path

    from parsers.form_bin_parser_v2 import V8FormBinParser
    from parsers.form_structure_parser import FormStructureParser

    cli = argparse.ArgumentParser(description='Генерация web JSON schema из Form.bin.')
    cli.add_argument('input_path', help='исходный Form.bin')
    cli.add_argument('output_path', help='выходной JSON')
    args = cli.parse_args()

    bin_parser = V8FormBinParser()
    form_data = bin_parser.parse(Path(args.input_path))
    parsed_form = FormStructureParser().parse_enriched(form_data.form_structure_text)

    FormWebSchemaGenerator().generate_to_file(parsed_form, args.output_path)
    print(f'Generated {args.output_path}')


if __name__ == '__main__':
    main()
