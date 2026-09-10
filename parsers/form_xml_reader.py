"""
Lightweight reader for 1C managed Form.xml files.

Extracts the structural facts needed for comparison against a generated
managed form: controls (name/type/data-path/command), attributes, commands,
and events. Uses only the standard library (xml.etree) — no external deps.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_NS = '{http://v8.1c.ru/8.3/xcf/logform}'


def _ln(tag: str) -> str:
    """Strip the namespace prefix from a tag."""
    return tag.split('}')[-1] if '}' in tag else tag


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or '').strip() if el is not None else ''


def _localized(el: Optional[ET.Element]) -> str:
    """Extract the first ru content from a multilang <v8:item> block."""
    if el is None:
        return ''
    for item in el.findall(f'{_NS}v8:item'):
        lang = item.find(f'{_NS}v8:lang')
        content = item.find(f'{_NS}v8:content')
        if lang is not None and _text(lang) == 'ru' and content is not None:
            return _text(content)
    return ''


@dataclass
class XmlControl:
    name: str = ""
    type: str = ""
    title: str = ""
    data_path: str = ""
    command: str = ""
    children: List['XmlControl'] = field(default_factory=list)


@dataclass
class XmlAttribute:
    name: str = ""
    type: str = ""
    main: bool = False


@dataclass
class XmlCommand:
    name: str = ""
    title: str = ""
    action: str = ""


@dataclass
class XmlEvent:
    name: str = ""
    handler: str = ""


@dataclass
class XmlForm:
    title: str = ""
    width: int = 0
    height: int = 0
    controls: List[XmlControl] = field(default_factory=list)
    attributes: List[XmlAttribute] = field(default_factory=list)
    commands: List[XmlCommand] = field(default_factory=list)
    events: List[XmlEvent] = field(default_factory=list)


class FormXmlReader:
    """Reads a managed Form.xml into a structural XmlForm."""

    def read(self, path: Path) -> XmlForm:
        root = ET.parse(str(path)).getroot()
        form = XmlForm()
        form.title = _localized(root.find(f'{_NS}Title'))
        w = root.find(f'{_NS}Width')
        h = root.find(f'{_NS}Height')
        form.width = int(_text(w)) if w is not None else 0
        form.height = int(_text(h)) if h is not None else 0

        # ChildItems (top-level controls)
        child_items = root.find(f'{_NS}ChildItems')
        if child_items is not None:
            for el in child_items:
                ctrl = self._read_control(el)
                if ctrl:
                    form.controls.append(ctrl)

        # Attributes
        attrs = root.find(f'{_NS}Attributes')
        if attrs is not None:
            for el in attrs.findall(f'{_NS}Attribute'):
                form.attributes.append(self._read_attribute(el))

        # Commands
        cmds = root.find(f'{_NS}Commands')
        if cmds is not None:
            for el in cmds.findall(f'{_NS}Command'):
                form.commands.append(self._read_command(el))

        # Events
        events = root.find(f'{_NS}Events')
        if events is not None:
            for el in events.findall(f'{_NS}Event'):
                form.events.append(XmlEvent(
                    name=el.get('name', ''),
                    handler=_text(el),
                ))

        return form

    def _read_control(self, el: ET.Element) -> Optional[XmlControl]:
        ctrl = XmlControl(
            name=el.get('name', ''),
            type=_ln(el.tag),
            title=_localized(el.find(f'{_NS}Title')),
            data_path=_text(el.find(f'{_NS}DataPath')),
            command=_text(el.find(f'{_NS}CommandName')),
        )
        child_items = el.find(f'{_NS}ChildItems')
        if child_items is not None:
            for child in child_items:
                sub = self._read_control(child)
                if sub:
                    ctrl.children.append(sub)
        return ctrl

    def _read_attribute(self, el: ET.Element) -> XmlAttribute:
        type_el = el.find(f'{_NS}Type')
        type_name = ''
        if type_el is not None:
            v8type = type_el.find(f'{_NS}v8:Type')
            type_name = _text(v8type)
        return XmlAttribute(
            name=el.get('name', ''),
            type=type_name,
            main=_text(el.find(f'{_NS}MainAttribute')) == 'true',
        )

    def _read_command(self, el: ET.Element) -> XmlCommand:
        return XmlCommand(
            name=el.get('name', ''),
            title=_localized(el.find(f'{_NS}Title')),
            action=_text(el.find(f'{_NS}Action')),
        )
