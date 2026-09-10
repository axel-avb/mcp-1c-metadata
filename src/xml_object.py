"""Parser for per-object XML files (<Type>.<Name>.xml and <Type>.<Name>/Forms/*.xml).

The dump unpacks each object into its own XML file(s); forms live under
`<ObjectDir>/Forms/<FormName>.xml`. These files carry the details missing from
the manifest: Name/Synonym/Comment and, critically, `FormType`
(Managed/Ordinary/Auto) which drives the is_legacy flag.

Tolerant: any broken/missing file yields an empty result, not an exception.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger(__name__)

_NS = "{http://v8.1c.ru/8.3/MDClasses}"

# FormType -> is_legacy (None = inherit the global run mode)
_FORM_TYPE_LEGACY: dict[str, bool | None] = {
    "Managed": False,
    "Ordinary": True,
    "Auto": None,
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def form_type_of(path: Path) -> str:
    """Return the FormType string of a Form XML file, or '' if absent/broken."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        log.warning("cannot read %s: %s", path, e)
        return ""
    try:
        root = ET.fromstring(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ET.ParseError) as e:
        log.warning("%s is not parseable: %s", path, e)
        return ""
    el = root.find(f".//{_NS}FormType")
    if el is None:
        el = root.find(".//FormType")
    return (el.text or "").strip() if el is not None else ""


def collect_form_types(object_dir: Path) -> list[str]:
    """Collect FormType values for all forms of an unpacked object directory."""
    forms_dir = object_dir / "Forms"
    if not forms_dir.is_dir():
        return []
    result: list[str] = []
    for f in sorted(forms_dir.glob("*.xml")):
        ft = form_type_of(f)
        if ft:
            result.append(ft)
    return result


def object_is_legacy(object_dir: Path, global_legacy: bool) -> bool:
    """Decide an object's legacy flag from its forms + the global run mode.

    Rules (PLAN.md §6):
      - any form with FormType=Ordinary  -> legacy (True)
      - else if global run mode is ordinary -> legacy (True, inherits)
      - else -> managed (False)
    """
    types = collect_form_types(object_dir)
    if "Ordinary" in types:
        return True
    if global_legacy:
        return True
    return False


def _find_legacy_forms(dump_dir: Path) -> list[Path]:
    """Find Form.bin files (ordinary/legacy forms) under an object's directory."""
    try:
        return sorted(dump_dir.rglob("Form.bin"))
    except OSError:
        return []


def parse_legacy_object(source_key: str, dump_dir: Path) -> dict | None:
    """Parse the legacy (ordinary) forms of one object via the vendored binary parser.

    Returns a dict with:
      * module_code — the BSL code of the form modules (concatenated),
      * structure_text — the curly-brace form structure text (concatenated),
      * form_count — number of Form.bin files found.

    The vendored parser lives in `parsers/` (from github.com/axel-avb/v8_ordinary_unpack).
    A missing/broken file simply contributes nothing (tolerant by design).
    """
    forms = _find_legacy_forms(dump_dir)
    if not forms:
        return None

    try:
        from parsers.form_bin_parser_v2 import V8FormBinParser  # type: ignore
    except ImportError as e:
        log.warning("legacy parser unavailable: %s", e)
        return None

    parser = V8FormBinParser()
    module_codes: list[str] = []
    structure_texts: list[str] = []
    for f in forms:
        try:
            result = parser.parse(f)
        except Exception as e:
            log.warning("legacy parse failed for %s: %s", f, e)
            continue
        if result.module_code and result.module_code.strip():
            module_codes.append(result.module_code)
        if result.form_structure_text and result.form_structure_text.strip():
            structure_texts.append(result.form_structure_text)

    if not module_codes and not structure_texts:
        return None

    return {
        "module_code": "\n".join(module_codes),
        "structure_text": "\n".join(structure_texts),
        "form_count": len(forms),
    }