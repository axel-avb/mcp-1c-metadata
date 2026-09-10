from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import logging
import time
import xml.etree.ElementTree as ET
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from xcf_utils import (
    NS,
    local_name,
    normalize_key,
    get_text,
    get_localized_text,
    parse_path_triplet,
    ru_category_from_folder,
    compute_form_qn,
    control_display_name,
    make_control_qn,
    make_event_qn,
    normalize_event_name,
    make_form_attr_qn,
    flatten_simple_children,
    normalize_properties_map,
    normalize_properties_values,
    ru_control_type,
)

from graphdb.console_search import build_console_search

logger = logging.getLogger(__name__)


class FormXmlParser:
    """
    Parser for 1C Managed Form XCF files (Ext/Form.xml).
    Produces RU-keyed property maps as required, with EN labels and future-proof relationships.
    """

    def parse_directory(self, code_dir: Path, project_name: str, config_name: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Scan code_dir for .../Forms/*/Ext/Form.xml and parse all found files.

        Streaming discovery + overlapped parsing using a small ThreadPool.

        Returns a dict of batched rows:
        {
          'form_updates': [{'form_qn', 'properties'}],
          'controls': [{'qn','name','type','properties'}],
          'root_rel': [{'form_qn','control_qn','order'}],
          'child_rel': [{'parent_qn','child_qn','order'}],
          'events': [{'qn','properties'}],
          'event_rel': [{'source_qn','event_qn'}],
          'form_attributes': [{'qn','name','properties'}],
        }
        """
        code_dir = code_dir.resolve()
        rows = {
            "form_updates": [],
            "controls": [],
            "root_rel": [],
            "child_rel": [],
            "events": [],
            "event_rel": [],
            "event_actions": [],
            "form_attributes": [],
            "form_commands": [],
            "form_command_usages": [],
        }

        logger.info("Streaming Form.xml scan under %s ...", code_dir)
        t0 = time.time()

        patterns = ["Forms/*/Ext/Form.xml", "CommonForms/*/Ext/Form.xml"]
        discovered = 0
        parsed_ok = 0
        parsed_err = 0

        # I/O-bound parsing typically benefits from a small pool
        max_workers = min(8, (os.cpu_count() or 4))
        max_in_flight = max_workers * 4  # simple backpressure

        def worker(fpath: Path) -> Optional[Dict[str, List[Dict[str, Any]]]]:
            try:
                triplet = parse_path_triplet(code_dir, fpath)
                if not triplet:
                    return None
                category_folder, object_name, form_name = triplet
                category_ru = ru_category_from_folder(category_folder)
                form_qn = compute_form_qn(project_name, config_name, category_ru, object_name, form_name)
                is_commonform = (category_ru == "ОбщиеФормы")

                parsed = self._parse_form_file(fpath, form_qn)
                if not parsed:
                    return None

                partial: Dict[str, List[Dict[str, Any]]] = {
                    "form_updates": [],
                    "controls": [],
                    "root_rel": [],
                    "child_rel": [],
                    "events": [],
                    "event_rel": [],
                    "event_actions": [],
                    "form_attributes": [],
                    "form_commands": [],
                    "form_command_usages": [],
                }

                # Form updates: also recompute search-fields, because Form.xml
                # carries `Заголовок`/`Синоним` that arrived AFTER rows_builder's
                # initial pass. For CommonForm the update writes to MetadataObject
                # (kind='object', section='objects'), and the real name is the
                # last segment of owner_qn (NOT the trailing `Форма`).
                if parsed.get("form_properties"):
                    fu_props = dict(parsed["form_properties"] or {})
                    # Stable bits (section, name, name_norm) can be computed from
                    # parser context. synonym/type are intentionally NOT computed
                    # here from delta-properties — Cypher recomputes them from the
                    # final node state to avoid wiping values that came earlier
                    # via rows_builder but are absent in this Form.xml delta.
                    if is_commonform:
                        owner_qn = form_qn.split("/Form/", 1)[0]
                        update_name = owner_qn.rsplit("/", 1)[-1]
                        cs = build_console_search(update_name, fu_props, "object")
                    else:
                        cs = build_console_search(form_name, fu_props, "form")
                    fu_props["console_search_section"] = cs["console_search_section"]
                    fu_props["console_search_name"] = cs["console_search_name"]
                    fu_props["console_search_name_norm"] = cs["console_search_name_norm"]
                    partial["form_updates"].append({
                        "form_qn": form_qn,
                        "properties": fu_props,
                    })

                # Controls and relations
                for c in parsed.get("controls", []):
                    c_props = dict(c.get("properties") or {})
                    c_name = c.get("name") or ""
                    c_props.setdefault("project_name", project_name)
                    c_props.setdefault("config_name", config_name)
                    c_props.update(build_console_search(c_name, c_props, "form_control"))
                    partial["controls"].append({
                        "qn": c["qn"],
                        "name": c_name,
                        "type": None,
                        "properties": c_props,
                    })
                for rel in parsed.get("root_rel", []):
                    partial["root_rel"].append(rel)
                for rel in parsed.get("child_rel", []):
                    partial["child_rel"].append(rel)

                # Events
                for e in parsed.get("events", []):
                    partial["events"].append({
                        "qn": e["qn"],
                        "properties": e.get("properties") or {},
                    })
                for er in parsed.get("event_rel", []):
                    partial["event_rel"].append(er)
                for ea in parsed.get("event_actions", []):
                    partial["event_actions"].append(ea)

                # Form attributes (attach form_qn here for loader convenience)
                for a in parsed.get("form_attributes", []):
                    a_props = dict(a.get("properties") or {})
                    a_name = a.get("name") or ""
                    a_props.setdefault("project_name", project_name)
                    a_props.setdefault("config_name", config_name)
                    a_props.update(build_console_search(a_name, a_props, "form_attribute"))
                    partial["form_attributes"].append({
                        "qn": a["qn"],
                        "form_qn": form_qn,
                        "name": a_name,
                        "properties": a_props,
                    })

                # Form-level commands
                for fc in parsed.get("form_commands", []):
                    fc_props = dict(fc.get("properties") or {})
                    fc_name = fc.get("cmd_name") or fc.get("name") or ""
                    fc_props.setdefault("project_name", project_name)
                    fc_props.setdefault("config_name", config_name)
                    fc_props.update(build_console_search(fc_name, fc_props, "command"))
                    fc_out = dict(fc)
                    fc_out["properties"] = fc_props
                    partial["form_commands"].append(fc_out)

                # Command usages (control -> command links)
                for fu in parsed.get("form_command_usages", []):
                    partial["form_command_usages"].append(fu)

                return partial
            except Exception as e:
                logger.error("Failed to parse form file %s: %s", fpath, e)
                return None

        def merge(partial: Optional[Dict[str, List[Dict[str, Any]]]]) -> None:
            nonlocal parsed_ok
            if not partial:
                return
            rows["form_updates"].extend(partial.get("form_updates", []))
            rows["controls"].extend(partial.get("controls", []))
            rows["root_rel"].extend(partial.get("root_rel", []))
            rows["child_rel"].extend(partial.get("child_rel", []))
            rows["events"].extend(partial.get("events", []))
            rows["event_rel"].extend(partial.get("event_rel", []))
            rows["event_actions"].extend(partial.get("event_actions", []))
            rows["form_attributes"].extend(partial.get("form_attributes", []))
            rows["form_commands"].extend(partial.get("form_commands", []))
            rows["form_command_usages"].extend(partial.get("form_command_usages", []))
            parsed_ok += 1

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            in_flight = set()
            for pattern in patterns:
                for fpath in code_dir.rglob(pattern):
                    discovered += 1
                    fut = executor.submit(worker, fpath)
                    in_flight.add(fut)
        
                    if (discovered % 100) == 0:
                        logger.info("Discovered %d Form.xml files so far...", discovered)
        
                    # Maintain backpressure when too many tasks are in-flight
                    if len(in_flight) >= max_in_flight:
                        try:
                            done = next(as_completed(in_flight))
                            in_flight.remove(done)
                            try:
                                partial = done.result()
                            except Exception as e:
                                parsed_err += 1
                                logger.error("Worker exception: %s", e)
                                partial = None
                            merge(partial)
                            if (parsed_ok % 50) == 0 and parsed_ok > 0:
                                logger.info("Parsed %d Form.xml files so far...", parsed_ok)
                        except StopIteration:
                            # Shouldn't happen since in_flight is non-empty
                            pass

            # Drain remaining futures
            for done in as_completed(in_flight):
                try:
                    partial = done.result()
                except Exception as e:
                    parsed_err += 1
                    logger.error("Worker exception: %s", e)
                    partial = None
                merge(partial)
                if (parsed_ok % 50) == 0 and parsed_ok > 0:
                    logger.info("Parsed %d Form.xml files so far...", parsed_ok)

        elapsed = time.time() - t0
        if discovered == 0:
            logger.info("Streaming scan finished in %.1fs, found 0 files under %s", elapsed, code_dir)
            return rows

        logger.info(
            "Streaming scan+parse finished in %.1fs: discovered=%d, parsed=%d, errors=%d",
            elapsed, discovered, parsed_ok, parsed_err
        )
        return rows

    def parse_files(self, file_paths: List[Path], project_name: str, config_name: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Parse provided list of Ext/Form.xml files (both .../Forms/*/Ext/Form.xml and .../CommonForms/*/Ext/Form.xml).
        Same parallel strategy as parse_directory, but without rglob() to support single-pass filesystem scanning.
        """
        rows = {
            "form_updates": [],
            "controls": [],
            "root_rel": [],
            "child_rel": [],
            "events": [],
            "event_rel": [],
            "event_actions": [],
            "form_attributes": [],
            "form_commands": [],
            "form_command_usages": [],
        }

        if not file_paths:
            return rows

        logger.info("Parsing Form.xml from provided file list: %d files", len(file_paths))
        t0 = time.time()

        discovered = 0
        parsed_ok = 0
        parsed_err = 0

        max_workers = min(8, (os.cpu_count() or 4))
        max_in_flight = max_workers * 4

        # Reuse same worker/merge logic
        def worker(fpath: Path) -> Optional[Dict[str, List[Dict[str, Any]]]]:
            try:
                # compute code_dir as root for relativity (parent of the root is okay because parse_path_triplet uses relative_to)
                code_dir = fpath.anchor and Path(fpath.anchor) or fpath.resolve().anchor
                # Use parent chain to resolve relative base as the provided code root is unknown here;
                # parse_path_triplet only needs code_dir to compute relative parts correctly.
                # We pass the real code root via best effort using fpath.parts up to 'code' segment if present.
                # Fallback: use fpath.parents[-1] (drive root) will make relative_to fail; handle in parse_path_triplet.
                # Better: rely on caller to provide valid file_paths originated from code_dir; so use commonpath parent
                # Here we just call with the parent of parent to allow relative_to succeed in our previous patterns.
                # However parse_path_triplet handles exceptions and returns None if not relative.
                potential_root = fpath
                # Walk up to find 'code' folder marker if present
                for p in fpath.parents:
                    if p.name == "code":
                        potential_root = p
                        break
                triplet = parse_path_triplet(potential_root, fpath)
                if not triplet:
                    # Try with immediate 'code_dir' passed as two levels up of Ext
                    triplet = parse_path_triplet((fpath.parent.parent.parent.parent if len(fpath.parents) >= 4 else fpath.parent.parent), fpath)
                    if not triplet:
                        return None
                category_folder, object_name, form_name = triplet
                category_ru = ru_category_from_folder(category_folder)
                form_qn = compute_form_qn(project_name, config_name, category_ru, object_name, form_name)
                is_commonform = (category_ru == "ОбщиеФормы")

                parsed = self._parse_form_file(fpath, form_qn)
                if not parsed:
                    return None

                partial: Dict[str, List[Dict[str, Any]]] = {
                    "form_updates": [],
                    "controls": [],
                    "root_rel": [],
                    "child_rel": [],
                    "events": [],
                    "event_rel": [],
                    "event_actions": [],
                    "form_attributes": [],
                    "form_commands": [],
                    "form_command_usages": [],
                }

                if parsed.get("form_properties"):
                    fu_props = dict(parsed["form_properties"] or {})
                    # Stable bits (section, name, name_norm) can be computed from
                    # parser context. synonym/type are intentionally NOT computed
                    # here from delta-properties — Cypher recomputes them from the
                    # final node state to avoid wiping values that came earlier
                    # via rows_builder but are absent in this Form.xml delta.
                    if is_commonform:
                        owner_qn = form_qn.split("/Form/", 1)[0]
                        update_name = owner_qn.rsplit("/", 1)[-1]
                        cs = build_console_search(update_name, fu_props, "object")
                    else:
                        cs = build_console_search(form_name, fu_props, "form")
                    fu_props["console_search_section"] = cs["console_search_section"]
                    fu_props["console_search_name"] = cs["console_search_name"]
                    fu_props["console_search_name_norm"] = cs["console_search_name_norm"]
                    partial["form_updates"].append({
                        "form_qn": form_qn,
                        "properties": fu_props,
                    })

                for c in parsed.get("controls", []):
                    c_props = dict(c.get("properties") or {})
                    c_name = c.get("name") or ""
                    c_props.setdefault("project_name", project_name)
                    c_props.setdefault("config_name", config_name)
                    c_props.update(build_console_search(c_name, c_props, "form_control"))
                    partial["controls"].append({
                        "qn": c["qn"],
                        "name": c_name,
                        "type": None,
                        "properties": c_props,
                    })
                for rel in parsed.get("root_rel", []):
                    partial["root_rel"].append(rel)
                for rel in parsed.get("child_rel", []):
                    partial["child_rel"].append(rel)

                for e in parsed.get("events", []):
                    partial["events"].append({
                        "qn": e["qn"],
                        "properties": e.get("properties") or {},
                    })
                for er in parsed.get("event_rel", []):
                    partial["event_rel"].append(er)
                for ea in parsed.get("event_actions", []):
                    partial["event_actions"].append(ea)

                for a in parsed.get("form_attributes", []):
                    a_props = dict(a.get("properties") or {})
                    a_name = a.get("name") or ""
                    a_props.setdefault("project_name", project_name)
                    a_props.setdefault("config_name", config_name)
                    a_props.update(build_console_search(a_name, a_props, "form_attribute"))
                    partial["form_attributes"].append({
                        "qn": a["qn"],
                        "form_qn": form_qn,
                        "name": a_name,
                        "properties": a_props,
                    })

                for fc in parsed.get("form_commands", []):
                    fc_props = dict(fc.get("properties") or {})
                    fc_name = fc.get("cmd_name") or fc.get("name") or ""
                    fc_props.setdefault("project_name", project_name)
                    fc_props.setdefault("config_name", config_name)
                    fc_props.update(build_console_search(fc_name, fc_props, "command"))
                    fc_out = dict(fc)
                    fc_out["properties"] = fc_props
                    partial["form_commands"].append(fc_out)
                for fu in parsed.get("form_command_usages", []):
                    partial["form_command_usages"].append(fu)

                return partial
            except Exception as e:
                logger.error("Failed to parse form file %s: %s", fpath, e)
                return None

        def merge(partial: Optional[Dict[str, List[Dict[str, Any]]]]) -> None:
            nonlocal parsed_ok
            if not partial:
                return
            rows["form_updates"].extend(partial.get("form_updates", []))
            rows["controls"].extend(partial.get("controls", []))
            rows["root_rel"].extend(partial.get("root_rel", []))
            rows["child_rel"].extend(partial.get("child_rel", []))
            rows["events"].extend(partial.get("events", []))
            rows["event_rel"].extend(partial.get("event_rel", []))
            rows["event_actions"].extend(partial.get("event_actions", []))
            rows["form_attributes"].extend(partial.get("form_attributes", []))
            rows["form_commands"].extend(partial.get("form_commands", []))
            rows["form_command_usages"].extend(partial.get("form_command_usages", []))
            parsed_ok += 1

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            in_flight = set()
            for fpath in file_paths:
                discovered += 1
                fut = executor.submit(worker, fpath)
                in_flight.add(fut)

                if len(in_flight) >= max_in_flight:
                    try:
                        done = next(as_completed(in_flight))
                        in_flight.remove(done)
                        try:
                            partial = done.result()
                        except Exception as e:
                            parsed_err += 1
                            logger.error("Worker exception: %s", e)
                            partial = None
                        merge(partial)
                        if (parsed_ok % 50) == 0 and parsed_ok > 0:
                            logger.info("Parsed %d Form.xml files so far...", parsed_ok)
                    except StopIteration:
                        pass

            for done in as_completed(in_flight):
                try:
                    partial = done.result()
                except Exception as e:
                    parsed_err += 1
                    logger.error("Worker exception: %s", e)
                    partial = None
                merge(partial)
                if (parsed_ok % 50) == 0 and parsed_ok > 0:
                    logger.info("Parsed %d Form.xml files so far...", parsed_ok)

        elapsed = time.time() - t0
        logger.info(
            "File-list Form.xml parse finished in %.1fs: discovered=%d, parsed=%d, errors=%d",
            elapsed, discovered, parsed_ok, parsed_err
        )
        return rows

    def _parse_form_file(self, file_path: Path, form_qn: str) -> Optional[Dict[str, Any]]:
        """Parse a single Ext/Form.xml into structures."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except Exception as e:
            logger.warning("Cannot parse XML %s: %s", file_path, e)
            return None
        result = self._parse_form_root(root, form_qn, base_id_index=None)
        if result is not None:
            child_items = next(
                (e for e in root if e.tag.split("}")[-1] == "ChildItems"), None
            )
            if child_items is not None:
                result["form_content_hash"] = self._inner_canonical_hash(child_items)
        return result

    def _parse_form_root(self, root: ET.Element, form_qn: str, base_id_index: Optional[Dict] = None, base_command_index: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """
        Parse a form XML root element into structures.
        base_id_index: None = base config (no ext_source written);
                       dict = extension (empty dict → all controls classified as own).
        base_command_index: None = base config; dict (name→elem) = extension (from BaseForm Commands).
        """
        out: Dict[str, Any] = {
            "form_properties": {},
            "controls": [],
            "root_rel": [],
            "child_rel": [],
            "events": [],
            "event_rel": [],
            "event_actions": [],
            "form_attributes": [],
            "form_commands": [],
            "form_command_usages": [],
        }

        # Form-level properties (flatten simple children; RU keys) + normalize values (bool/enums)
        out["form_properties"] = normalize_properties_values(
            flatten_simple_children(root, node_label="Form"),
            node_label="Form",
        )

        # Form-level events
        self._extract_events_for_node(root, form_qn, "", out)

        # Form Attributes (XCF <Attributes>)
        attrs_block = root.find("form:Attributes", NS)
        if attrs_block is None:
            attrs_block = root.find("Attributes")
        if attrs_block is not None:
            for attr in list(attrs_block):
                if not isinstance(attr.tag, str):
                    continue
                if local_name(attr.tag) != "Attribute":
                    continue
                a_name = attr.get("name") or ""
                a_qn = make_form_attr_qn(form_qn, a_name)
                props = self._extract_attribute_props(attr)
                props["config_name"] = form_qn.split("/")[1]
                props["content_hash"] = self._element_canonical_hash(attr)
                out["form_attributes"].append({
                    "qn": a_qn,
                    "name": a_name,
                    "properties": props,
                })

        # Form-level Commands (<Commands><Command .../>)
        cmds_block = root.find("form:Commands", NS)
        if cmds_block is None:
            cmds_block = root.find("Commands")
        if cmds_block is not None:
            for cmd in list(cmds_block):
                if not isinstance(cmd.tag, str) or local_name(cmd.tag) != "Command":
                    continue
                cmd_name = (cmd.get("name") or "").strip()
                if not cmd_name:
                    continue
                cmd_qn = f"{form_qn}/Command/{cmd_name}"
                props: Dict[str, Any] = {}

                # Basic attributes
                cid = (cmd.get("id") or "").strip()
                if cid:
                    props[normalize_key("id", node_label="Command")] = cid
                props[normalize_key("name", node_label="Command")] = cmd_name

                # Localized Title / ToolTip (namespaced; explicit None checks to avoid Element truthiness warnings)
                title_el = cmd.find("form:Title", NS)
                if title_el is None:
                    title_el = cmd.find(".//form:Title", NS)
                title_txt = get_localized_text(title_el)
                if title_txt:
                    props[normalize_key("Title", node_label="Command")] = title_txt

                tooltip_el = cmd.find("form:ToolTip", NS)
                if tooltip_el is None:
                    tooltip_el = cmd.find(".//form:ToolTip", NS)
                tooltip_txt = get_localized_text(tooltip_el)
                if tooltip_txt:
                    props[normalize_key("ToolTip", node_label="Command")] = tooltip_txt

                # Action elements — plain string for no-callType, JSON array for callType actions
                import json as _json
                action_els = cmd.findall("form:Action", NS)
                if not action_els:
                    action_els = cmd.findall(".//form:Action", NS)
                if action_els:
                    ct_actions = []
                    for a_el in action_els:
                        handler = get_text(a_el)
                        if not handler:
                            continue
                        ct = (a_el.get("callType") or "").strip()
                        if ct:
                            ct_actions.append({"callType": ct, "handler": handler})
                        else:
                            props[normalize_key("Action", node_label="Command")] = handler
                    if ct_actions:
                        props[normalize_key("Action", node_label="Command")] = _json.dumps(ct_actions, ensure_ascii=False)
                        props["action_handlers"] = [a["handler"] for a in ct_actions]

                # ext_source + modified_props for extension forms
                if base_command_index is not None:
                    ext_source = self._classify_command_ext_source(cmd, base_command_index)
                    props["ext_source"] = ext_source
                    if ext_source == "adopted_modified":
                        modified = self._get_command_modified_properties(
                            cmd, base_command_index.get(cmd_name)
                        )
                        if modified:
                            props["modified_properties"] = modified

                props = normalize_properties_values(props, node_label="Command")
                props["config_name"] = form_qn.split("/")[1]
                out["form_commands"].append({
                    "form_qn": form_qn,
                    "cmd_qn": cmd_qn,
                    "cmd_name": cmd_name,
                    "properties": props or {},
                })

        # Controls tree under <ChildItems>
        child_items = root.find("form:ChildItems", NS)
        if child_items is None:
            child_items = root.find("ChildItems")
        if child_items is not None:
            controls = [ch for ch in list(child_items) if isinstance(ch.tag, str)]
            for idx, ctrl in enumerate(controls):
                self._walk_control(ctrl, form_qn, parent_qn=None, parent_path=[], index=idx, out=out,
                                   base_id_index=base_id_index)

        return out

    def _extract_attribute_props(self, elem: ET.Element) -> Dict[str, Any]:
        """
        Extract properties for a <Attribute> inside Form/Attributes.
        """
        props: Dict[str, Any] = {}
        # name/id as properties too (RU keys)
        nm = (elem.get("name") or "").strip()
        if nm:
            props[normalize_key("name", node_label="FormAttribute")] = nm
        cid = (elem.get("id") or "").strip()
        if cid:
            props[normalize_key("id", node_label="FormAttribute")] = cid

        # Heuristic flatten for simple scalar children
        props.update(flatten_simple_children(elem, node_label="FormAttribute"))
 
        # Try to extract 'Type' block textual content if present
        t = elem.find(".//v8:Type", NS)
        if t is not None:
            tval = "".join((t.text or "").split())
            if tval:
                props[normalize_key("Type", node_label="FormAttribute")] = t.text.strip()
 
        # Normalize values (bools/enums/etc.)
        props = normalize_properties_values(props, node_label="FormAttribute")
        return props

    _CALL_TYPE_RU = {
        "Main":     "Основной",
        "Before":   "Перед",
        "After":    "После",
        "Override": "Вместо",
    }

    def _extract_events_for_node(self, elem: ET.Element, form_qn: str, target_path: str, out: Dict[str, Any], source_qn: Optional[str] = None):
        """
        Extract <Events><Event name="..."/> blocks for either Form or a specific control.
        One FormEvent per unique event_qn; one FormEventAction per XML <Event> row.
        """
        events_block = elem.find("form:Events", NS)
        if events_block is None:
            events_block = elem.find("Events")
        if events_block is None:
            return

        config_name = form_qn.split("/")[1]
        src_qn = source_qn if source_qn else form_qn
        source_label = "FormControl" if source_qn else "Form"

        seen_events: set = set()
        seen_rels: set = set()

        for ev in list(events_block):
            if not isinstance(ev.tag, str) or local_name(ev.tag) != "Event":
                continue
            ev_name_en = (ev.get("name") or "").strip()
            if not ev_name_en:
                continue
            ru_ev_name = normalize_event_name(ev_name_en)
            handler = normalize_event_name(get_text(ev))
            raw_call_type = (ev.get("callType") or "").strip()
            call_type = raw_call_type if raw_call_type in ("Before", "After", "Override") else "Main"
            call_type_ru = self._CALL_TYPE_RU[call_type]

            ev_qn = make_event_qn(form_qn, target_path, ev_name_en)
            action_qn = f"{ev_qn}/Action/{call_type}"

            # Deduplicate FormEvent by event_qn
            if ev_qn not in seen_events:
                seen_events.add(ev_qn)
                ev_props = {
                    normalize_key("name_attr"): ru_ev_name,
                    normalize_key("name"): ru_ev_name,
                    "event_name": ev_name_en,
                    "config_name": config_name,
                }
                ev_props = normalize_properties_values(ev_props)
                out["events"].append({"qn": ev_qn, "properties": ev_props})

            # Deduplicate HAS_EVENT edge by (source_qn, event_qn)
            rel_key = (src_qn, ev_qn)
            if rel_key not in seen_rels:
                seen_rels.add(rel_key)
                out["event_rel"].append({
                    "source_qn": src_qn,
                    "source_label": source_label,
                    "event_qn": ev_qn,
                })

            # One FormEventAction per XML row (no deduplication — each callType is unique per event)
            out["event_actions"].append({
                "event_qn": ev_qn,
                "action_qn": action_qn,
                "properties": {
                    "name": call_type_ru,
                    "call_type": call_type,
                    "call_type_ru": call_type_ru,
                    "handler_name": handler,
                    "config_name": config_name,
                },
            })

    def _walk_control(self, elem: ET.Element, form_qn: str, parent_qn: Optional[str], parent_path: List[str], index: int, out: Dict[str, Any], base_id_index: Optional[Dict] = None, parent_name_path: Optional[List[str]] = None):
        """
        Recursively process a control node: collect properties, events, and hierarchy.
        """
        tag = local_name(elem.tag)
        control_type = ru_control_type(tag)
        display = control_display_name(elem)  # e.g., 'НомерСчета#7'
        current_path = parent_path + [display]
        ctrl_qn = make_control_qn(form_qn, current_path)

        # Base RU properties: name/id/type + simple scalars
        props: Dict[str, Any] = {}
        nm = elem.get("name")
        if nm:
            props[normalize_key("name", node_label="FormControl", control_type=control_type)] = nm.strip()
        cid = elem.get("id")
        if cid:
            props[normalize_key("id", node_label="FormControl", control_type=control_type)] = cid.strip()
        props[normalize_key("ТипКонтрола", node_label="FormControl", control_type=control_type)] = tag  # RU key, value = tag name
 
        # Merge simple child properties
        props.update(flatten_simple_children(
            elem,
            extra_extract={normalize_key("Порядок", node_label="FormControl", control_type=control_type): index},
            node_label="FormControl",
            control_type=control_type,
        ))

        # Capture raw DataPath (un-normalized) alongside normalized 'ПутьКДанным'
        try:
            dp_elem = elem.find("form:DataPath", NS)
            if dp_elem is None:
                dp_elem = elem.find("DataPath")
            dp_raw = get_text(dp_elem)
            if dp_raw:
                # Keep raw as-is; normalization pipeline won't alter this key
                props["ПутьКДанным_RAW"] = dp_raw.strip()
        except Exception:
            # Be resilient to any malformed nodes
            pass
 
        # Also expose control type under generic key 'Тип' with RU value
        props[normalize_key("Тип", node_label="FormControl", control_type=control_type)] = control_type
 
        # Normalize values (bool/enums/control type, etc.)
        props = normalize_properties_values(props, node_label="FormControl", control_type=control_type)

        # ctrl_id: integer stored AFTER normalize_properties_values to avoid "1"→"Истина" translation
        raw_cid = elem.get("id")
        if raw_cid is not None:
            try:
                props["ctrl_id"] = int(raw_cid)
            except ValueError:
                pass

        # name_path: hierarchical path of pure names without #id suffix
        current_name_path = (parent_name_path or []) + [nm or local_name(elem.tag)]
        props["name_path"] = "/".join(current_name_path)
        props["config_name"] = form_qn.split("/")[1]

        # ext_source classification (only for extension forms; base_id_index=None skips this)
        if base_id_index is not None:
            ext_src = self._classify_ext_source(elem, base_id_index)
            props["ext_source"] = ext_src
            if ext_src != "own" and raw_cid is not None:
                try:
                    props["base_control_id"] = int(raw_cid)
                except ValueError:
                    pass
            if ext_src == "adopted_modified" and raw_cid is not None:
                try:
                    base_elem = base_id_index.get(int(raw_cid))
                    if base_elem is not None:
                        modified = self._get_modified_properties(elem, base_elem)
                        if modified:
                            props["modified_properties"] = modified
                except (ValueError, Exception):
                    pass

        # Register control node
        out["controls"].append({
            "qn": ctrl_qn,
            "name": nm or "",
            "type": None,
            "properties": props,
        })

        # Root or child relation
        if parent_qn:
            out["child_rel"].append({
                "parent_qn": parent_qn,
                "child_qn": ctrl_qn,
                "order": index,
            })
        else:
            out["root_rel"].append({
                "form_qn": form_qn,
                "control_qn": ctrl_qn,
                "order": index,
            })

        # Control-level events
        target_path = "/".join(current_path)
        self._extract_events_for_node(elem, form_qn, target_path, out, source_qn=ctrl_qn)

        # Command usages on this control (<CommandName>…</CommandName>)
        cmd_name_elem = elem.find("form:CommandName", NS)
        if cmd_name_elem is None:
            cmd_name_elem = elem.find("CommandName")
        if cmd_name_elem is not None:
            raw_qname = get_text(cmd_name_elem)
            if raw_qname:
                # Local commands are scoped to form; others use canonical full name as QN
                if raw_qname.startswith("Form.Command."):
                    local = raw_qname.split("Form.Command.", 1)[1]
                    cmd_qn = f"{form_qn}/Command/{local}" if local else raw_qname
                else:
                    cmd_qn = raw_qname

                rel_key = f"{ctrl_qn}|{cmd_qn}"
                out["form_command_usages"].append({
                    "form_qn": form_qn,
                    "container_qn": ctrl_qn,
                    "cmd_qn": cmd_qn,
                    "rel_key": rel_key,
                    "via": tag,
                    "button_id": (elem.get("id") or "").strip(),
                    "button_name": (elem.get("name") or "").strip(),
                })

        # Recurse into children
        child_items = elem.find("form:ChildItems", NS)
        if child_items is None:
            child_items = elem.find("ChildItems")
        if child_items is not None:
            children = [ch for ch in list(child_items) if isinstance(ch.tag, str)]
            for idx, ch in enumerate(children):
                self._walk_control(ch, form_qn, parent_qn=ctrl_qn, parent_path=current_path, index=idx, out=out,
                                   base_id_index=base_id_index, parent_name_path=current_name_path)

    # ---- Extension form parsing ----

    def parse_extension_form_file(
        self, file_path: Path, form_qn: str, is_adopted: bool
    ) -> Optional[Dict]:
        """
        Parse an extension Form.xml.
        is_adopted=True:  classify controls via BaseForm snapshot; compute base_form_hash.
        is_adopted=False: all controls get ext_source='own' (empty base_id_index).
        """
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
        except Exception as e:
            logger.warning("Cannot parse XML %s: %s", file_path, e)
            return None

        if not is_adopted:
            return self._parse_form_root(root, form_qn, base_id_index={})

        bf = next((e for e in root if e.tag.endswith("BaseForm")), None)
        base_id_index = self._build_base_id_index(bf) if bf is not None else {}

        base_command_index: Dict[str, ET.Element] = {}
        if bf is not None:
            bf_cmds = next((e for e in bf if e.tag.split("}")[-1] == "Commands"), None)
            if bf_cmds is not None:
                for cmd_el in bf_cmds:
                    if isinstance(cmd_el.tag, str) and cmd_el.tag.split("}")[-1] == "Command":
                        cname = (cmd_el.get("name") or "").strip()
                        if cname:
                            base_command_index[cname] = cmd_el

        result = self._parse_form_root(root, form_qn, base_id_index=base_id_index, base_command_index=base_command_index)

        if result is not None and bf is not None:
            # Hash only <ChildItems> of BaseForm — the only section present in both
            # BaseForm snapshot and the live base form, making hashes comparable.
            bf_child_items = next(
                (e for e in bf if e.tag.split("}")[-1] == "ChildItems"), None
            )
            if bf_child_items is not None:
                result["base_form_hash"] = self._inner_canonical_hash(bf_child_items)
        return result

    def _build_base_id_index(self, base_form_elem: ET.Element) -> Dict[int, ET.Element]:
        """
        Build {id: elem} index from <ChildItems> of <BaseForm> only.
        Full BaseForm can have id collisions between controls and attributes,
        so we scope to <ChildItems> where ids are unique (verified on HRM data).
        """
        index: Dict[int, ET.Element] = {}
        child_items = next(
            (e for e in base_form_elem if e.tag.split("}")[-1] == "ChildItems"), None
        )
        if child_items is None:
            return index
        for elem in child_items.iter():
            raw_id = elem.get("id")
            if raw_id is not None:
                try:
                    index[int(raw_id)] = elem
                except ValueError:
                    pass
        return index

    def _classify_ext_source(
        self, ctrl_elem: ET.Element, base_id_index: Dict[int, ET.Element]
    ) -> str:
        """
        Classify a control relative to BaseForm:
          own              — id not in base_id_index (includes empty index)
          adopted_unchanged — id matches and canonical hash is identical
          adopted_modified  — id matches but content differs
        """
        raw_id = ctrl_elem.get("id")
        if raw_id is None:
            return "own"
        try:
            ctrl_id = int(raw_id)
        except ValueError:
            return "own"

        base_elem = base_id_index.get(ctrl_id)
        if base_elem is None:
            return "own"

        return (
            "adopted_unchanged"
            if self._element_canonical_hash(ctrl_elem) == self._element_canonical_hash(base_elem)
            else "adopted_modified"
        )

    @staticmethod
    def _get_modified_properties(ctrl_elem: ET.Element, base_elem: ET.Element) -> List[str]:
        """
        Returns sorted deduplicated list of RU property names that differ between
        ctrl_elem (extension) and base_elem (base form snapshot).
        Covers: tag change, name attribute, direct non-ChildItems children, ChildItems subtree.
        """
        modified = []

        # 1. Tag change (e.g. InputField → LabelField)
        if ctrl_elem.tag.split("}")[-1] != base_elem.tag.split("}")[-1]:
            modified.append("ТипКонтрола")

        # 2. name attribute change
        if ctrl_elem.get("name") != base_elem.get("name"):
            ctrl_type = ru_control_type(ctrl_elem.tag.split("}")[-1])
            modified.append(normalize_key("name", node_label="FormControl", control_type=ctrl_type))

        # 3. Direct children except ChildItems (translated to RU via normalize_key)
        SKIP = {"ChildItems"}

        def _props_map(elem: ET.Element) -> Dict[str, ET.Element]:
            return {
                e.tag.split("}")[-1]: e
                for e in elem
                if e.tag.split("}")[-1] not in SKIP
            }

        ext_map = _props_map(ctrl_elem)
        base_map = _props_map(base_elem)

        for key in sorted(set(ext_map) | set(base_map)):
            ext_e = ext_map.get(key)
            base_e = base_map.get(key)
            if ext_e is None or base_e is None:
                ctrl_type = ru_control_type(ctrl_elem.tag.split("}")[-1])
                modified.append(normalize_key(key, node_label="FormControl", control_type=ctrl_type))
            elif (FormXmlParser._element_canonical_hash(ext_e)
                  != FormXmlParser._element_canonical_hash(base_e)):
                ctrl_type = ru_control_type(ctrl_elem.tag.split("}")[-1])
                modified.append(normalize_key(key, node_label="FormControl", control_type=ctrl_type))

        # 4. ChildItems subtree
        ext_ci = next((e for e in ctrl_elem if e.tag.split("}")[-1] == "ChildItems"), None)
        base_ci = next((e for e in base_elem if e.tag.split("}")[-1] == "ChildItems"), None)
        if ext_ci is not None or base_ci is not None:
            ext_h = FormXmlParser._inner_canonical_hash(ext_ci) if ext_ci is not None else ""
            base_h = FormXmlParser._inner_canonical_hash(base_ci) if base_ci is not None else ""
            if ext_h != base_h:
                modified.append("ДочерниеКонтролы")

        return sorted(set(modified))

    @staticmethod
    def _inner_canonical_hash(elem: ET.Element) -> str:
        """sha1 of canonical XML of all CHILDREN of elem (no id attributes).
        Used for ChildItems-level form sync detection."""
        import copy
        import hashlib
        import io
        buf = io.StringIO()
        for child in elem:
            c = copy.deepcopy(child)
            for e in c.iter():
                e.attrib.pop("id", None)
            ET.canonicalize(ET.tostring(c, encoding="unicode"), out=buf, strip_text=True)
        return hashlib.sha1(buf.getvalue().encode()).hexdigest()

    @staticmethod
    def _element_canonical_hash(elem: ET.Element) -> str:
        """sha1 of canonical XML of elem itself (tag + all attrs except id + children).
        Used for control and FormAttribute comparison."""
        import copy
        import hashlib
        import io
        c = copy.deepcopy(elem)
        for e in c.iter():
            e.attrib.pop("id", None)
        buf = io.StringIO()
        ET.canonicalize(ET.tostring(c, encoding="unicode"), out=buf, strip_text=True)
        return hashlib.sha1(buf.getvalue().encode()).hexdigest()

    def _classify_command_ext_source(
        self, cmd_elem: ET.Element, base_command_index: Dict[str, ET.Element]
    ) -> str:
        """Classify extension form Command as own/adopted_unchanged/adopted_modified.

        Compares only children PRESENT in extension (delta representation):
        absent children are inherited, not changed. Handles multiple elements
        with the same tag (e.g. multiple <Action> with different callType).
        """
        from collections import defaultdict
        cmd_name = (cmd_elem.get("name") or "").strip()
        base_elem = base_command_index.get(cmd_name)
        if base_elem is None:
            return "own"
        base_by_tag: Dict[str, list] = defaultdict(list)
        for e in base_elem:
            if isinstance(e.tag, str):
                base_by_tag[e.tag.split("}")[-1]].append(e)
        seen_tags: set = set()
        for ext_child in cmd_elem:
            if not isinstance(ext_child.tag, str):
                continue
            tag = ext_child.tag.split("}")[-1]
            if tag in seen_tags:
                continue
            seen_tags.add(tag)
            ext_els = [
                c for c in cmd_elem
                if isinstance(c.tag, str) and c.tag.split("}")[-1] == tag
            ]
            base_els = base_by_tag.get(tag, [])
            if len(ext_els) != len(base_els):
                return "adopted_modified"
            for ext_e, base_e in zip(ext_els, base_els):
                if self._element_canonical_hash(ext_e) != self._element_canonical_hash(base_e):
                    return "adopted_modified"
        return "adopted_unchanged"

    def _get_command_modified_properties(
        self, cmd_elem: ET.Element, base_elem: Optional[ET.Element]
    ) -> List[str]:
        """Return sorted list of RU property names that differ between extension and BaseForm command."""
        from collections import defaultdict
        if base_elem is None:
            return []
        base_by_tag: Dict[str, list] = defaultdict(list)
        for e in base_elem:
            if isinstance(e.tag, str):
                base_by_tag[e.tag.split("}")[-1]].append(e)
        modified = []
        seen_tags: set = set()
        for ext_child in cmd_elem:
            if not isinstance(ext_child.tag, str):
                continue
            tag = ext_child.tag.split("}")[-1]
            if tag in seen_tags:
                continue
            seen_tags.add(tag)
            ext_els = [
                c for c in cmd_elem
                if isinstance(c.tag, str) and c.tag.split("}")[-1] == tag
            ]
            base_els = base_by_tag.get(tag, [])
            if len(ext_els) != len(base_els):
                modified.append(normalize_key(tag, node_label="Command"))
            else:
                for ext_e, base_e in zip(ext_els, base_els):
                    if self._element_canonical_hash(ext_e) != self._element_canonical_hash(base_e):
                        modified.append(normalize_key(tag, node_label="Command"))
                        break
        return sorted(set(modified))
