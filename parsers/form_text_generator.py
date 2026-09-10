"""
Human-readable text generator for 1C form structure.

Produces a tree-style text description from ParsedForm AST.
"""
from __future__ import annotations

from typing import Dict, List

from parsers.form_structure_parser import (
    FormCommand,
    FormControl,
    FormEvent,
    FormTreeNode,
    ParsedForm,
)
from parsers.form_type_mapper import EVENT_TYPE_MAP


class FormTextGenerator:
    """
    Generates human-readable text description from ParsedForm.

    Usage:
        generator = FormTextGenerator()
        text = generator.generate(parsed_form)
    """

    def generate(self, form: ParsedForm, module_code: str = "") -> str:
        """Generate text description of the form."""
        lines: List[str] = []

        # Header
        form_type = "обычная"
        lines.append(f'Форма: "{form.title}" ({form_type})')
        lines.append(f'Размер: {form.width} x {form.height}')
        lines.append('')

        # Commands
        commands = self._collect_commands(form)
        if commands:
            lines.append('Команды:')
            for cmd in commands:
                title = cmd.title or cmd.name
                lines.append(f'  - {cmd.name} ({title})')
            lines.append('')

        # Attributes
        if form.attributes:
            lines.append('Атрибуты формы:')
            for attr in form.attributes:
                type_str = attr.type_1c or attr.type_pattern or '?'
                lines.append(f'  - {attr.name} ({type_str})')
            lines.append('')

        # Form events
        if form.events:
            lines.append('События формы:')
            seen_events = set()
            for evt in form.events:
                if evt.handler_method:
                    key = (evt.event_type_id, evt.control_index, evt.handler_method)
                    if key in seen_events:
                        continue
                    seen_events.add(key)
                    lines.append(f'  - {evt.event_name_ru} -> {evt.handler_method}')
            lines.append('')

        # Controls tree
        if form.controls:
            lines.append('Элементы управления:')
            self._gen_tree(form, lines)
            lines.append('')

        # Module code (if provided)
        if module_code:
            lines.append('Модуль формы:')
            lines.append(module_code)

        return '\n'.join(lines)

    def _collect_commands(self, form: ParsedForm) -> List[FormCommand]:
        """Collect unique form commands from the parsed AST."""
        return list(form.commands)

    def _gen_tree(self, form: ParsedForm, lines: List[str]):
        """Generate the controls tree from the bbox-containment tree."""
        if not form.tree:
            return
        for node in form.tree.children:
            is_last = (node == form.tree.children[-1])
            self._gen_node(node, lines, prefix='', is_last=is_last)

    def _gen_node(
        self,
        node: FormTreeNode,
        lines: List[str],
        prefix: str = '',
        is_last: bool = True,
    ):
        """Generate a single tree node."""
        connector = '└── ' if is_last else '├── '
        child_prefix = '    ' if is_last else '│   '

        ctrl = node.ctrl
        if ctrl is None:
            # Synthetic group node (not backed by a real control)
            name = f'[Группа {node.index}]'
            tag = '(группа)'
        else:
            tag = ctrl.xml_tag or 'Control'
            name = ctrl.name or f'[{ctrl.index}]'

        info = f'{name} ({tag})'

        if ctrl is not None:
            if ctrl.command_name:
                info += f' cmd={ctrl.command_name}'
            if ctrl.data_path:
                info += f' DataPath={ctrl.data_path}'
            if ctrl.width > 0 and ctrl.height > 0:
                info += f' [{ctrl.width}x{ctrl.height}]'

        lines.append(f'{prefix}{connector}{info}')

        if ctrl is not None and ctrl.events:
            seen_events = set()
            for evt in ctrl.events:
                if evt.handler_method:
                    key = (evt.event_type_id, evt.handler_method)
                    if key in seen_events:
                        continue
                    seen_events.add(key)
                    evt_prefix = prefix + ('    ' if is_last else '│   ')
                    lines.append(
                        f'{evt_prefix}  ↳ {evt.event_name_ru} -> {evt.handler_method}'
                    )

        for i, child_node in enumerate(node.children):
            child_is_last = (i == len(node.children) - 1)
            self._gen_node(
                child_node, lines,
                prefix=prefix + child_prefix, is_last=child_is_last,
            )
