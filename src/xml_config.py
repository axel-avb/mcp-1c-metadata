"""Parser for Configuration.xml (1C configuration root, format 2.13).

Extracts the global run-mode / compatibility flags that drive the is_legacy
detection (see PLAN.md §6):

    <DefaultRunMode>OrdinaryApplication|ManagedApplication|Auto</DefaultRunMode>
    <InterfaceCompatibilityMode>Taxi|Version8_2|...</InterfaceCompatibilityMode>
    <CompatibilityMode>Version8_3_20|...</CompatibilityMode>

Tolerant: a missing/broken file yields defaults (managed mode) rather than
raising.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger(__name__)

_NS = "{http://v8.1c.ru/8.3/MDClasses}"

_RUN_MODE = {
    "OrdinaryApplication": "ordinary",
    "ManagedApplication": "managed",
    "Auto": "auto",
}


def parse_configuration(xml_root: Path) -> dict[str, str]:
    """Read Configuration.xml flags.

    Returns dict with keys: run_mode ('ordinary'|'managed'|'auto'),
    interface_mode, compatibility_mode. Defaults to managed/auto/empty on any
    failure.
    """
    path = Path(xml_root) / "Configuration.xml"
    try:
        raw = path.read_bytes()
    except OSError as e:
        log.warning("cannot read %s: %s", path, e)
        return _defaults()

    try:
        root = ET.fromstring(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ET.ParseError) as e:
        log.warning("%s is not parseable: %s", path, e)
        return _defaults()

    def _find(local_name: str) -> str:
        el = root.find(f".//{_NS}{local_name}")
        if el is None:
            el = root.find(f".//{local_name}")
        return (el.text or "").strip() if el is not None else ""

    run_raw = _find("DefaultRunMode")
    return {
        "run_mode": _RUN_MODE.get(run_raw, "managed" if not run_raw else "auto"),
        "interface_mode": _find("InterfaceCompatibilityMode"),
        "compatibility_mode": _find("CompatibilityMode"),
    }


def _defaults() -> dict[str, str]:
    return {"run_mode": "managed", "interface_mode": "", "compatibility_mode": ""}


def global_is_legacy(flags: dict[str, str]) -> bool:
    """True if the whole configuration defaults to ordinary (legacy) mode."""
    return flags.get("run_mode") == "ordinary"
