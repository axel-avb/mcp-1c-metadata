"""
Parser for 1C predefined values (Predefined.xml) from configuration code dump.
Produces rows for batch loading into Neo4j as PredefinedItem nodes.

Conventions:
- Properties use Russian keys without spaces, per requirement.
- Linking to owner MetadataObject is done by category_name + object_name in loader.
- QualifiedName for PredefinedItem is constructed in loader as:
    m.qualified_name + '/Predef/' + row.local_id
- Child hierarchy for ChartsOfAccounts is emitted via relation rows with parent_local_id.

Categories supported:
- Catalogs                   -> Справочники
- ChartsOfAccounts           -> ПланыСчетов
- ChartsOfCharacteristicTypes-> ПланыВидовХарактеристик
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import logging
import xml.etree.ElementTree as ET
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

NS = {
    "predef": "http://v8.1c.ru/8.3/xcf/predef",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

DIR_TO_CATEGORY = {
    "Catalogs": "Справочники",    
    "ChartsOfAccounts": "ПланыСчетов",
    "ChartsOfCharacteristicTypes": "ПланыВидовХарактеристик",
}

ACCOUNT_TYPE_MAP = {
    "Active": "Активный",
    "Passive": "Пассивный",
    "ActivePassive": "АктивноПассивный",
}

TYPE_HEAD_MAP = {
    "CatalogRef": "Справочники",
    "DocumentRef": "Документы",
    "EnumRef": "Перечисления",
    "InformationRegisterRef": "РегистрыСведений",
    "AccumulationRegisterRef": "РегистрыНакопления",
    "ChartOfCharacteristicTypesRef": "ПланыВидовХарактеристик",
    "ChartOfAccountsRef": "ПланыСчетов",
}


def _to_bool(text: Optional[str]) -> Optional[bool]:
    if text is None:
        return None
    t = text.strip().lower()
    if t == "true":
        return True
    if t == "false":
        return False
    return None


def _last_segment(dot_path: str) -> str:
    if not dot_path:
        return ""
    parts = dot_path.split(".")
    return parts[-1].strip() if parts else dot_path.strip()


def _clean_ns_prefix(s: str) -> str:
    # remove "d4p1:" style prefix if present
    if ":" in s:
        return s.split(":", 1)[1]
    return s


def _normalize_type_literal(raw: Optional[str]) -> Optional[str]:
    """
    Convert like 'd4p1:CatalogRef.Билеты' -> 'Справочники.Билеты'
    If head is unknown, return cleaned original without ns prefix.
    """
    if not raw:
        return None
    s = _clean_ns_prefix(raw).strip()
    if "." not in s:
        return s
    head, tail = s.split(".", 1)
    head = head.strip()
    tail = tail.strip()
    mapped = TYPE_HEAD_MAP.get(head)
    return f"{mapped}.{tail}" if mapped and tail else s


def _sanitize_local_id(name: str) -> str:
    """
    Fallback local id when XML has no @id. Keep Russian letters, Latin, digits, underscore.
    Replace others with underscore. Prefix with 'path:' to mark non-GUID origin.
    """
    out_chars: List[str] = []
    for ch in (name or ""):
        if ch.isalnum() or ch == "_":
            out_chars.append(ch)
        else:
            out_chars.append("_")
    cleaned = "".join(out_chars).strip("_") or "unnamed"
    return f"path:{cleaned}"


class PredefinedParser:
    """
    Parse /app/data/code/.../Ext/Predefined.xml files into rows:
    - items: List[{
        'category_name': str,
        'object_name': str,
        'local_id': str,
        'properties': Dict[str, any]
      }]
    - relations: List[{
        'category_name': str,
        'object_name': str,
        'parent_local_id': str,
        'local_id': str
      }]
    """

    def parse_directory(self, code_dir: Path) -> Tuple[List[Dict], List[Dict]]:
        """
        Streamed discovery and overlapped parsing for Predefined.xml:
        - Do not pre-materialize the full list (no list(...)).
        - Iterate rglob generator and dispatch parse tasks to a small ThreadPool.
        - Merge results on the main thread; log discovery and parsing progress periodically.
        """
        items: List[Dict] = []
        relations: List[Dict] = []

        if not code_dir.exists():
            logger.warning("Code directory with Predefined.xml is missing: %s", code_dir)
            return items, relations

        logger.info("Scanning Predefined.xml under %s", code_dir)
        t0 = time.time()

        discovered = 0
        parsed_files = 0

        # I/O-bound XML parsing typically benefits from a small pool
        max_workers = min(8, (os.cpu_count() or 4))
        max_in_flight = max_workers * 4  # simple backpressure

        def worker(xml_path: Path) -> Tuple[List[Dict], List[Dict]]:
            """
            Parse a single Predefined.xml and return (items, relations) produced from it.
            Category is derived from directory, falling back to xsi:type sniffing.
            """
            local_items: List[Dict] = []
            local_rel: List[Dict] = []
            try:
                # Expect path .../<Dir>/<ObjectName>/Ext/Predefined.xml
                obj_dir = xml_path.parent.parent  # .../<ObjectName>
                dir_dir = obj_dir.parent          # .../<Dir>
                object_name = obj_dir.name
                dir_name = dir_dir.name
                category_name = DIR_TO_CATEGORY.get(dir_name)

                root = None
                if not category_name:
                    # Fallback: detect type from XML root
                    try:
                        tree = ET.parse(xml_path)
                        root = tree.getroot()
                        xsi_type = root.attrib.get(f"{{{NS['xsi']}}}type", "")
                        if xsi_type.endswith("CatalogPredefinedItems"):
                            category_name = "Справочники"
                        elif xsi_type.endswith("PlanOfCharacteristicKindPredefinedItems"):
                            category_name = "ПланыВидовХарактеристик"
                        elif xsi_type.endswith("ChartOfAccountsPredefinedItems"):
                            category_name = "ПланыСчетов"
                    except Exception:
                        pass

                if not category_name:
                    logger.debug("Skipping Predefined.xml under unsupported dir: %s", xml_path)
                    return local_items, local_rel

                # Ensure we have XML root to avoid double-parse in common case
                if root is None:
                    tree = ET.parse(xml_path)
                    root = tree.getroot()

                xsi_type = root.attrib.get(f"{{{NS['xsi']}}}type", "")

                if xsi_type.endswith("CatalogPredefinedItems"):
                    self._parse_catalog_items(root, category_name, object_name, local_items)
                elif xsi_type.endswith("PlanOfCharacteristicKindPredefinedItems"):
                    self._parse_pochk_items(root, category_name, object_name, local_items)
                elif xsi_type.endswith("ChartOfAccountsPredefinedItems"):
                    self._parse_chart_of_accounts_items(root, category_name, object_name, local_items, local_rel)
                else:
                    # Heuristic fallback: default to Catalogs-like items
                    self._parse_catalog_items(root, category_name, object_name, local_items)

            except Exception as e:
                logger.error("Failed to parse %s: %s", xml_path, e)

            return local_items, local_rel

        files_iter = code_dir.rglob("Predefined.xml")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            in_flight = set()
            for xml_path in files_iter:
                discovered += 1
                fut = executor.submit(worker, xml_path)
                in_flight.add(fut)

                if (discovered % 100) == 0:
                    logger.info("Discovered %d Predefined.xml files so far...", discovered)

                # Backpressure: when too many tasks in-flight, merge one completion
                if len(in_flight) >= max_in_flight:
                    try:
                        done = next(as_completed(in_flight))
                        in_flight.remove(done)
                        try:
                            loc_items, loc_rel = done.result()
                        except Exception as e:
                            logger.error("Worker exception: %s", e)
                            loc_items, loc_rel = [], []
                        if loc_items:
                            items.extend(loc_items)
                        if loc_rel:
                            relations.extend(loc_rel)
                        parsed_files += 1
                        if (parsed_files % 50) == 0:
                            logger.info("Parsed Predefined.xml files: %d (items=%d, relations=%d)", parsed_files, len(items), len(relations))
                    except StopIteration:
                        pass

            # Drain remaining futures
            for done in as_completed(in_flight):
                try:
                    loc_items, loc_rel = done.result()
                except Exception as e:
                    logger.error("Worker exception: %s", e)
                    loc_items, loc_rel = [], []
                if loc_items:
                    items.extend(loc_items)
                if loc_rel:
                    relations.extend(loc_rel)
                parsed_files += 1
                if (parsed_files % 50) == 0:
                    logger.info("Parsed Predefined.xml files: %d (items=%d, relations=%d)", parsed_files, len(items), len(relations))

        elapsed = time.time() - t0
        logger.info(
            "Parsed Predefined.xml: files=%d, items=%d, relations=%d, elapsed=%.1fs",
            parsed_files, len(items), len(relations), elapsed
        )
        return items, relations

    def parse_files(self, file_paths: List[Path]) -> Tuple[List[Dict], List[Dict]]:
        """
        Parse a provided list of Predefined.xml files with the same streamed/parallel strategy.
        Used by the single-pass code scan to avoid a second rglob over the filesystem.
        """
        items: List[Dict] = []
        relations: List[Dict] = []

        if not file_paths:
            return items, relations

        logger.info("Parsing Predefined.xml from provided file list: %d files", len(file_paths))
        t0 = time.time()

        discovered = 0
        parsed_files = 0

        max_workers = min(8, (os.cpu_count() or 4))
        max_in_flight = max_workers * 4

        def worker(xml_path: Path) -> Tuple[List[Dict], List[Dict]]:
            local_items: List[Dict] = []
            local_rel: List[Dict] = []
            try:
                # Expect path .../<Dir>/<ObjectName>/Ext/Predefined.xml
                obj_dir = xml_path.parent.parent  # .../<ObjectName>
                dir_dir = obj_dir.parent          # .../<Dir>
                object_name = obj_dir.name
                dir_name = dir_dir.name
                category_name = DIR_TO_CATEGORY.get(dir_name)

                root = None
                if not category_name:
                    try:
                        tree = ET.parse(xml_path)
                        root = tree.getroot()
                        xsi_type = root.attrib.get(f"{{{NS['xsi']}}}type", "")
                        if xsi_type.endswith("CatalogPredefinedItems"):
                            category_name = "Справочники"
                        elif xsi_type.endswith("PlanOfCharacteristicKindPredefinedItems"):
                            category_name = "ПланыВидовХарактеристик"
                        elif xsi_type.endswith("ChartOfAccountsPredefinedItems"):
                            category_name = "ПланыСчетов"
                    except Exception:
                        pass

                if not category_name:
                    logger.debug("Skipping Predefined.xml under unsupported dir: %s", xml_path)
                    return local_items, local_rel

                if root is None:
                    tree = ET.parse(xml_path)
                    root = tree.getroot()

                xsi_type = root.attrib.get(f"{{{NS['xsi']}}}type", "")

                if xsi_type.endswith("CatalogPredefinedItems"):
                    self._parse_catalog_items(root, category_name, object_name, local_items)
                elif xsi_type.endswith("PlanOfCharacteristicKindPredefinedItems"):
                    self._parse_pochk_items(root, category_name, object_name, local_items)
                elif xsi_type.endswith("ChartOfAccountsPredefinedItems"):
                    self._parse_chart_of_accounts_items(root, category_name, object_name, local_items, local_rel)
                else:
                    self._parse_catalog_items(root, category_name, object_name, local_items)
            except Exception as e:
                logger.error("Failed to parse %s: %s", xml_path, e)

            return local_items, local_rel

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            in_flight = set()
            for xml_path in file_paths:
                discovered += 1
                fut = executor.submit(worker, xml_path)
                in_flight.add(fut)

                if len(in_flight) >= max_in_flight:
                    try:
                        done = next(as_completed(in_flight))
                        in_flight.remove(done)
                        try:
                            loc_items, loc_rel = done.result()
                        except Exception as e:
                            logger.error("Worker exception: %s", e)
                            loc_items, loc_rel = [], []
                        if loc_items:
                            items.extend(loc_items)
                        if loc_rel:
                            relations.extend(loc_rel)
                        parsed_files += 1
                    except StopIteration:
                        pass

            for done in as_completed(in_flight):
                try:
                    loc_items, loc_rel = done.result()
                except Exception as e:
                    logger.error("Worker exception: %s", e)
                    loc_items, loc_rel = [], []
                if loc_items:
                    items.extend(loc_items)
                if loc_rel:
                    relations.extend(loc_rel)
                parsed_files += 1

        elapsed = time.time() - t0
        logger.info(
            "Parsed Predefined.xml (file-list): files=%d, items=%d, relations=%d, elapsed=%.1fs",
            parsed_files, len(items), len(relations), elapsed
        )
        return items, relations

    def _parse_predefined_file(
        self,
        xml_path: Path,
        category_name: str,
        object_name: str,
        items: List[Dict],
        relations: List[Dict],
    ):
        tree = ET.parse(xml_path)
        root = tree.getroot()
        xsi_type = root.attrib.get(f"{{{NS['xsi']}}}type", "")

        if xsi_type.endswith("CatalogPredefinedItems"):
            self._parse_catalog_items(root, category_name, object_name, items)
        elif xsi_type.endswith("PlanOfCharacteristicKindPredefinedItems"):
            self._parse_pochk_items(root, category_name, object_name, items)
        elif xsi_type.endswith("ChartOfAccountsPredefinedItems"):
            self._parse_chart_of_accounts_items(root, category_name, object_name, items, relations)
        else:
            # Heuristic fallback: detect by first Item structure
            # Default to Catalogs-like simple items
            self._parse_catalog_items(root, category_name, object_name, items)

    def _base_item_props(self, item_el) -> Dict[str, object]:
        name = (item_el.findtext("predef:Name", default="", namespaces=NS) or "").strip()
        code = (item_el.findtext("predef:Code", default="", namespaces=NS) or "").strip()
        description = (item_el.findtext("predef:Description", default="", namespaces=NS) or "").strip()
        is_folder = _to_bool(item_el.findtext("predef:IsFolder", default=None, namespaces=NS))
        props: Dict[str, object] = {
            "Имя": name,
            "Код": code,
            "Наименование": description,
        }
        if is_folder is not None:
            props["Группа"] = is_folder
        return props

    def _parse_catalog_items(self, root, category_name: str, object_name: str, items: List[Dict]):
        for it in root.findall("predef:Item", NS):
            props = self._base_item_props(it)
            local_id = it.attrib.get("id") or _sanitize_local_id(props.get("Имя") or "")
            items.append({
                "category_name": category_name,
                "object_name": object_name,
                "local_id": local_id,
                "properties": props,
            })

    def _parse_pochk_items(self, root, category_name: str, object_name: str, items: List[Dict]):
        for it in root.findall("predef:Item", NS):
            props = self._base_item_props(it)
            # Тип
            raw_type = it.findtext("predef:Type/v8:Type", default="", namespaces=NS).strip()
            norm = _normalize_type_literal(raw_type) if raw_type else None
            if norm:
                props["Тип"] = norm
            local_id = it.attrib.get("id") or _sanitize_local_id(props.get("Имя") or "")
            items.append({
                "category_name": category_name,
                "object_name": object_name,
                "local_id": local_id,
                "properties": props,
            })

    def _parse_chart_of_accounts_items(
        self,
        root,
        category_name: str,
        object_name: str,
        items: List[Dict],
        relations: List[Dict],
    ):
        def parse_item_recursive(el, parent_local_id: Optional[str] = None):
            props = self._base_item_props(el)

            # ТипСчета
            acct_type = (el.findtext("predef:AccountType", default="", namespaces=NS) or "").strip()
            if acct_type:
                props["ТипСчета"] = ACCOUNT_TYPE_MAP.get(acct_type, acct_type)

            # Забалансовый
            off_balance = el.findtext("predef:OffBalance", default=None, namespaces=NS)
            b = _to_bool(off_balance)
            if b is not None:
                props["Забалансовый"] = b

            # Порядок
            order = el.findtext("predef:Order", default="", namespaces=NS)
            if order:
                props["Порядок"] = order

            # AccountingFlags
            for flag in el.findall("predef:AccountingFlags/predef:Flag", NS):
                ref = flag.attrib.get("ref", "")  # e.g. ChartOfAccounts.Хозрасчетный.AccountingFlag.Валютный
                flag_name = _last_segment(ref)
                fval = _to_bool(flag.text or "")
                if flag_name and fval is not None:
                    # only known flags likely, but keep general
                    props[flag_name] = fval

            # ExtDimensionTypes -> ВидыСубконто (list of last segments)
            ext_names: List[str] = []
            for ext in el.findall("predef:ExtDimensionTypes/predef:ExtDimensionType", NS):
                full_name = (ext.attrib.get("name") or "").strip()
                last = _last_segment(full_name)
                if last and last not in ext_names:
                    ext_names.append(last)
            if ext_names:
                props["ВидыСубконто"] = ext_names

            local_id = el.attrib.get("id") or _sanitize_local_id(props.get("Имя") or "")

            # Emit item
            items.append({
                "category_name": category_name,
                "object_name": object_name,
                "local_id": local_id,
                "properties": props,
            })

            # If has parent, emit relation
            if parent_local_id:
                relations.append({
                    "category_name": category_name,
                    "object_name": object_name,
                    "parent_local_id": parent_local_id,
                    "local_id": local_id,
                })

            # Recurse child items
            for child in el.findall("predef:ChildItems/predef:Item", NS):
                parse_item_recursive(child, parent_local_id=local_id)

        for it in root.findall("predef:Item", NS):
            parse_item_recursive(it, parent_local_id=None)