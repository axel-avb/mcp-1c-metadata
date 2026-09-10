"""
Comparison report: generated managed form vs reference managed form.

Compares structure only — controls, attributes, commands, events, hierarchy.
Module code is compared only in the dimension of bound event handlers
(event name <-> method name), not the body.

The generated side comes from a ParsedForm AST (legacy Form.bin); the
reference side comes from a managed Form.xml via FormXmlReader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from parsers.form_structure_parser import ParsedForm
from parsers.form_xml_reader import XmlForm


@dataclass
class DiffItem:
    kind: str  # "control" | "attribute" | "command" | "event" | "hierarchy"
    name: str
    field: str
    generated: Any = None
    reference: Any = None


@dataclass
class ComparisonReport:
    form_name: str = ""
    diffs: List[DiffItem] = field(default_factory=list)
    generated_control_count: int = 0
    reference_control_count: int = 0

    @property
    def clean(self) -> bool:
        return not self.diffs

    def add(self, kind: str, name: str, field: str, gen: Any, ref: Any):
        self.diffs.append(DiffItem(kind, name, field, gen, ref))


def _flatten_controls(controls: List[Any]) -> List[Any]:
    """Flatten a nested control tree into a flat list.

    Tree nodes (FormTreeNode) are unwrapped to their underlying control
    (FormControl / XmlControl) so field access is uniform.
    """
    flat: List[Any] = []
    for c in controls:
        node = getattr(c, 'ctrl', None)
        flat.append(node if node is not None else c)
        flat.extend(_flatten_controls(getattr(c, 'children', [])))
    return flat


def _control_key(c: Any) -> str:
    return getattr(c, 'name', '') or ''


def compare_forms(
    generated: ParsedForm,
    reference: XmlForm,
    form_name: str = "",
) -> ComparisonReport:
    """Compare a generated ParsedForm against a reference managed XmlForm."""
    report = ComparisonReport(form_name=form_name)

    gen_controls = _flatten_controls(generated.tree.children) if generated.tree else []
    ref_controls = _flatten_controls(reference.controls)
    report.generated_control_count = len(gen_controls)
    report.reference_control_count = len(ref_controls)

    gen_by_name = {_control_key(c): c for c in gen_controls}
    ref_by_name = {_control_key(c): c for c in ref_controls}

    # Controls present in reference but missing in generated (or vice versa)
    for name in ref_by_name:
        if name not in gen_by_name:
            report.add("control", name, "missing", None, "present")
    for name in gen_by_name:
        if name not in ref_by_name:
            report.add("control", name, "extra", "present", None)

    # Control field comparison (type, data path, command)
    for name in ref_by_name:
        if name not in gen_by_name:
            continue
        g = gen_by_name[name]
        r = ref_by_name[name]
        g_type = getattr(g, 'xml_tag', '') or ''
        r_type = getattr(r, 'type', '') or ''
        if g_type != r_type:
            report.add("control", name, "type", g_type, r_type)
        g_dp = getattr(g, 'data_path', '') or ''
        r_dp = getattr(r, 'data_path', '') or ''
        if g_dp != r_dp:
            report.add("control", name, "dataPath", g_dp, r_dp)
        g_cmd = getattr(g, 'command_name', '') or ''
        r_cmd = getattr(r, 'command', '') or ''
        if g_cmd != r_cmd:
            report.add("control", name, "command", g_cmd, r_cmd)

    # Attributes
    gen_attrs = {a.name: a for a in generated.attributes}
    ref_attrs = {a.name: a for a in reference.attributes}
    for name in ref_attrs:
        if name not in gen_attrs:
            report.add("attribute", name, "missing", None, "present")
    for name in gen_attrs:
        if name not in ref_attrs:
            report.add("attribute", name, "extra", "present", None)
    for name in ref_attrs:
        if name not in gen_attrs:
            continue
        g = gen_attrs[name]
        r = ref_attrs[name]
        g_main = name == generated.main_attribute
        if g_main != r.main:
            report.add("attribute", name, "main", g_main, r.main)

    # Commands
    gen_cmds = {c.name: c for c in generated.commands}
    ref_cmds = {c.name: c for c in reference.commands}
    for name in ref_cmds:
        if name not in gen_cmds:
            report.add("command", name, "missing", None, "present")
    for name in gen_cmds:
        if name not in ref_cmds:
            report.add("command", name, "extra", "present", None)

    # Events — bound handlers only (event name <-> method name)
    gen_events = {(e.event_name_en or e.event_name_ru, e.handler_method)
                  for e in generated.events if e.handler_method}
    ref_events = {(e.name, e.handler) for e in reference.events if e.handler}
    for ev in ref_events:
        if ev not in gen_events:
            report.add("event", ev[0], "handler", None, ev[1])
    for ev in gen_events:
        if ev not in ref_events:
            report.add("event", ev[0], "handler", ev[1], None)

    return report


def render_report(report: ComparisonReport) -> str:
    """Render a ComparisonReport as human-readable text."""
    lines: List[str] = []
    lines.append(f"Сравнение формы: {report.form_name or '(без имени)'}")
    lines.append(f"  Контролов: сгенерировано {report.generated_control_count}, "
                 f"эталон {report.reference_control_count}")
    if report.clean:
        lines.append("  Результат: РАСХОЖДЕНИЙ НЕТ (чисто)")
    else:
        lines.append(f"  Результат: {len(report.diffs)} расхождений")
        for d in report.diffs:
            lines.append(
                f"  - [{d.kind}] {d.name}.{d.field}: "
                f"сгенерировано={d.generated!r}, эталон={d.reference!r}"
            )
    return "\n".join(lines)
