"""Indexer: 1C configuration sources -> SQLite graph + Qdrant vectors.

Usage:
    python -m src.indexer [--config config.json] [--full] [--no-vectors]

Incremental by default: each node's content hash is stored in SQLite; only
changed nodes are re-embedded and upserted into Qdrant. Stale nodes (removed
from the configuration) are deleted from both stores.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .bs_parser import parse_bs_file, parse_bs_text, infer_module_role
from .config import AppConfig, load_config
from .config_parser import (
    ConfigElement,
    ConfigObject,
    element_text_repr,
    object_text_repr,
    stable_id,
)
from .report_parser import parse_report
from .embedder import Embedder
from .graph import GraphStore
from .xml_manifest import parse_dumpinfo, source_key_for
from .xml_config import parse_configuration
from .xml_object import object_is_legacy, parse_legacy_object

log = logging.getLogger(__name__)


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass
class EmbeddingItem:
    node_id: str
    text: str
    payload: dict = field(default_factory=dict)


def _element_id(obj: ConfigObject, elem: ConfigElement, path: str) -> str:
    return stable_id("element", obj.node_id, f"{path}/{elem.name}", elem.elem_type)


def _file_checksum(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _merge_manifest(objects: list[ConfigObject], cfg: AppConfig) -> dict[str, str]:
    """Enrich txt-derived objects with XML-manifest metadata.

    Merges by source_key (English object name). The .txt report stays the
    source of truth for names/synonyms; the manifest adds source_key,
    english_type, config_version and id.

    Returns the global flags from Configuration.xml (for is_legacy detection).
    """
    xml_root = cfg.resolve_xml_root()
    manifest = parse_dumpinfo(xml_root / "ConfigDumpInfo.xml")
    flags = parse_configuration(xml_root)

    if not manifest:
        log.info("no XML manifest at %s; indexing txt layer only", xml_root)
        return flags

    matched = 0
    for obj in objects:
        sk = source_key_for(obj.type, obj.short_name)
        info = manifest.get(sk)
        if info is None:
            # maybe the txt used a different short-name casing; try name-based
            for msk, minfo in manifest.items():
                if msk.split(".", 1)[-1].lower() == obj.short_name.lower():
                    info = minfo
                    sk = msk
                    break
        if info is not None:
            obj.source_key = sk
            obj.english_type = info["english_type"]
            obj.config_version = info["config_version"]
            matched += 1
        else:
            obj.source_key = sk  # best-effort source_key even without manifest entry

    log.info("manifest enrich: %d/%d objects matched", matched, len(objects))
    return flags


def collect_graph(cfg: AppConfig):
    """Parse the .txt report (ФАЗА 0) + XML manifest + BSL.

    Returns (nodes, edges, hashes, embedding_items, n_objects, n_bsl, flags, object_checksums).
    """
    report_path = cfg.resolve_txt_root() / "ОтчетПоКонфигурации.txt"
    objects = parse_report(report_path)
    log.info("parsed %d configuration objects from %s", len(objects), report_path)

    flags = _merge_manifest(objects, cfg)
    global_legacy = flags.get("run_mode") == "ordinary"

    nodes: list[dict] = []
    edges: list[tuple[str, str, str]] = []
    hashes: dict[str, str] = {}
    embed_items: list[EmbeddingItem] = []

    obj_by_name: dict[str, str] = {obj.name: stable_id("object", obj.node_id) for obj in objects}
    obj_by_short: dict[str, list[str]] = {}
    for obj in objects:
        obj_by_short.setdefault(obj.short_name, []).append(obj_by_name[obj.name])

    # ---- objects -----------------------------------------------------------
    def add_element(
        obj: ConfigObject, elem: ConfigElement, parent_id: str, edge_kind: str, path: str
    ) -> None:
        eid = _element_id(obj, elem, path)
        parent = path.lstrip("/").replace("/", " › ")
        text = element_text_repr(obj, elem, parent_path=parent)
        nodes.append({
            "id": eid, "kind": "element", "name": elem.name, "object_name": obj.name,
            "type": elem.elem_type,
            "props": {"synonym": elem.synonym, "comment": elem.comment,
                      "data_type": elem.data_type, "ref_object": elem.ref_object,
                      "parent": parent},
        })
        hashes[eid] = _hash("element", text)
        pl = {"kind": "element", "name": elem.name, "object": obj.name,
              "type": elem.elem_type, "data_type": elem.data_type,
              "comment": elem.comment, "parent": parent}
        if cfg.node_id_in_payload:
            pl["node_id"] = eid
        embed_items.append(EmbeddingItem(eid, text, payload=pl))
        edges.append((parent_id, eid, edge_kind))
        if elem.ref_object and elem.ref_object in obj_by_name:
            edges.append((eid, obj_by_name[elem.ref_object], "REFERENCE"))
        for child in elem.children:
            add_element(obj, child, eid, "CHILD_ELEMENT", f"{path}/{elem.name}")

    for obj in objects:
        oid = obj_by_name[obj.name]
        text = object_text_repr(obj)
        legacy = _object_legacy(cfg, obj, global_legacy)
        nodes.append({
            "id": oid, "kind": "object", "name": obj.name, "object_name": obj.name,
            "type": obj.type,
            "props": {"synonym": obj.synonym, "comment": obj.comment,
                      "source_key": obj.source_key,
                      "english_type": obj.english_type,
                      "config_version": obj.config_version,
                      "is_legacy": legacy},
        })
        hashes[oid] = _hash("object", text)
        payload = {"kind": "object", "name": obj.name, "type": obj.type,
                   "synonym": obj.synonym, "comment": obj.comment,
                   "is_legacy": legacy}
        if obj.source_key:
            payload["source_key"] = obj.source_key
        if cfg.node_id_in_payload:
            payload["node_id"] = oid
        embed_items.append(EmbeddingItem(oid, text, payload=payload))
        for elem in obj.elements:
            add_element(obj, elem, oid, "HAS_ELEMENT", "")

    # ---- BSL modules, symbols, CALLS / USES --------------------------------
    xml_root = cfg.resolve_xml_root()
    bs_files: list[Path] = sorted(xml_root.rglob("*.bsl")) or sorted(xml_root.rglob("*.bs"))
    if not bs_files:
        bs_files = sorted(cfg.config_root.rglob("*.bsl")) or sorted(cfg.config_root.rglob("*.bs"))
    log.info("found %d BSL files", len(bs_files))

    symbol_ids_by_name: dict[str, list[str]] = {}
    parsed_modules = []  # (module_path, module) for the CALLS pass
    bsl_hashes_by_obj: dict[str, list[str]] = {}  # object_name -> list of file hashes

    for f in bs_files:
        role, object_name = infer_module_role(f.parent, f.name)
        owner_obj_id = obj_by_name.get(object_name, "")
        module = parse_bs_file(f, module_role=role, object_name=object_name)
        parsed_modules.append((f, module))

        rel = _rel_path(f, xml_root)
        bsl_hashes_by_obj.setdefault(object_name, []).append(_file_checksum(f))
        mid = stable_id("module", rel)
        nodes.append({
            "id": mid, "kind": "module", "name": rel, "object_name": object_name,
            "type": role, "props": {"owner_object_id": owner_obj_id},
        })

        for sym in module.symbols:
            sid = stable_id("symbol", sym.qualified_name, rel)
            symbol_ids_by_name.setdefault(sym.name, []).append(sid)
            nodes.append({
                "id": sid, "kind": "symbol", "name": sym.name,
                "object_name": object_name, "type": sym.kind,
                "props": {"visibility": sym.visibility, "line": sym.line,
                          "params": sym.params, "module": rel,
                          "body": sym.body},
            })
            hashes[sid] = _hash("symbol", sym.name, sym.body)
            pl = {"kind": "symbol", "name": sym.name, "object": object_name,
                  "visibility": sym.visibility, "line": sym.line, "module": rel}
            if cfg.node_id_in_payload:
                pl["node_id"] = sid
            embed_items.append(EmbeddingItem(
                sid,
                f"{sym.kind} {sym.name}({sym.params}) — {object_name}, модуль: {role}\n{sym.body}",
                payload=pl,
            ))
            edges.append((mid, sid, "DEFINES"))

        for prefix, short in module.object_refs:
            for target in obj_by_short.get(short, []):
                edges.append((mid, target, "USES"))
        if owner_obj_id:
            edges.append((owner_obj_id, mid, "HAS_MODULE"))

    # ---- legacy (ordinary) forms: extract BSL code from Form.bin ------------
    legacy_forms = _collect_legacy_form_modules(cfg, objects)
    for rel, object_name, owner_obj_id, code in legacy_forms:
        f = Path(rel)
        role = f"form:{f.parent.parent.name if f.parent and f.parent.parent else f.stem}"
        module = parse_bs_text(code, f, module_role=role, object_name=object_name)
        if not module.symbols:
            continue
        parsed_modules.append((f, module))

        mid = stable_id("module", rel)
        nodes.append({
            "id": mid, "kind": "module", "name": rel, "object_name": object_name,
            "type": role, "props": {"owner_object_id": owner_obj_id, "is_legacy": True},
        })

        for sym in module.symbols:
            sid = stable_id("symbol", sym.qualified_name, rel)
            symbol_ids_by_name.setdefault(sym.name, []).append(sid)
            nodes.append({
                "id": sid, "kind": "symbol", "name": sym.name,
                "object_name": object_name, "type": sym.kind,
                "props": {"visibility": sym.visibility, "line": sym.line,
                          "params": sym.params, "module": rel,
                          "body": sym.body, "is_legacy": True},
            })
            hashes[sid] = _hash("symbol", sym.name, sym.body)
            pl = {"kind": "symbol", "name": sym.name, "object": object_name,
                  "visibility": sym.visibility, "line": sym.line, "module": rel,
                  "is_legacy": True}
            if cfg.node_id_in_payload:
                pl["node_id"] = sid
            embed_items.append(EmbeddingItem(
                sid,
                f"{sym.kind} {sym.name}({sym.params}) — {object_name}, модуль: {role}\n{sym.body}",
                payload=pl,
            ))
            edges.append((mid, sid, "DEFINES"))

        if owner_obj_id:
            edges.append((owner_obj_id, mid, "HAS_MODULE"))

    # CALLS edges (symbol -> symbol, resolved by name in the global namespace)
    seen_calls: set[tuple[str, str]] = set()
    for f, module in parsed_modules:
        rel = _rel_path(f, xml_root)
        for sym in module.symbols:
            if not sym.calls:
                continue
            sid = stable_id("symbol", sym.qualified_name, rel)
            for callee in sorted(sym.calls):
                for target in symbol_ids_by_name.get(callee, []):
                    key = (sid, target)
                    if target == sid or key in seen_calls:
                        continue
                    seen_calls.add(key)
                    edges.append((sid, target, "CALLS"))

    # object-level checksums: sha256(source_key ∥ config_version ∥ bsl_hashes)
    object_checksums: dict[str, str] = {}
    for obj in objects:
        bsl_hashes = bsl_hashes_by_obj.get(obj.name, [])
        object_checksums[obj.source_key] = _hash(
            obj.source_key, obj.config_version, "\n".join(bsl_hashes)
        )

    return nodes, edges, hashes, embed_items, len(objects), len(bs_files), flags, object_checksums


def _rel_path(f: Path, base: Path) -> str:
    try:
        return str(f.relative_to(base))
    except ValueError:
        return str(f)


def _collect_legacy_form_modules(cfg: AppConfig, objects: list[ConfigObject]):
    """Yield (rel, object_name, owner_obj_id, code) for legacy Form.bin modules.

    Legacy (ordinary) forms store their BSL code inside a binary Form.bin rather
    than a .bsl file. We extract it via the vendored parser (parsers/) and turn
    each form into a virtual BSL module so its procedures join the symbol layer.
    """
    xml_root = cfg.resolve_xml_root()
    obj_by_name = {obj.name: stable_id("object", obj.node_id) for obj in objects}

    try:
        from parsers.form_bin_parser_v2 import V8FormBinParser  # type: ignore
    except ImportError as e:
        log.warning("legacy parser unavailable: %s", e)
        return

    parser = V8FormBinParser()
    for obj in objects:
        if not obj.source_key:
            continue
        from .xml_manifest import folder_for_type
        folder = folder_for_type(obj.english_type or obj.source_key.split(".", 1)[0])
        short = obj.source_key.split(".", 1)[-1]
        obj_dir = xml_root / folder / short
        if not obj_dir.is_dir():
            continue
        owner_obj_id = obj_by_name.get(obj.name, "")
        for f in sorted(obj_dir.rglob("Form.bin")):
            try:
                result = parser.parse(f)
            except Exception as e:
                log.warning("legacy parse failed for %s: %s", f, e)
                continue
            if not result.module_code or not result.module_code.strip():
                continue
            rel = _rel_path(f, xml_root)
            yield rel, obj.name, owner_obj_id, result.module_code


def _object_legacy(cfg: AppConfig, obj: ConfigObject, global_legacy: bool) -> bool:
    """Decide is_legacy for one object from its Form files + global run mode."""
    if not obj.source_key:
        return global_legacy
    # source_key "Catalog.Колледжи" -> folder "Catalogs/Колледжи"
    from .xml_manifest import folder_for_type
    folder = folder_for_type(obj.english_type or obj.source_key.split(".", 1)[0])
    short = obj.source_key.split(".", 1)[-1]
    obj_dir = cfg.resolve_xml_root() / folder / short
    if not obj_dir.is_dir():
        return global_legacy
    return object_is_legacy(obj_dir, global_legacy)


def _filter_object(
    nodes: list[dict],
    edges: list[tuple[str, str, str]],
    hashes: dict[str, str],
    embed_items: list[EmbeddingItem],
    object_checksums: dict[str, str],
    name: str,
):
    """Keep only nodes/edges belonging to a single object.

    `name` may be the Russian full/short name (Справочник.Колледжи / Колледжи)
    or the English source_key (Catalog.Колледжи).
    """
    obj_id = None
    for n in nodes:
        if n["kind"] != "object":
            continue
        p = n.get("props") or {}
        if name in (n["name"], n["object_name"], p.get("source_key", "")):
            obj_id = n["id"]
            break
    if obj_id is None:
        # short-name match
        for n in nodes:
            if n["kind"] != "object":
                continue
            if n["name"].split(".", 1)[-1] == name:
                obj_id = n["id"]
                break
    if obj_id is None:
        return [], [], {}, [], {}

    keep_ids = {obj_id}
    # Which edge kinds stay within a single object vs cross-object references.
    # REFERENCE/USES/CALLS point to other objects/symbols and must be excluded
    # from a single-object reindex.
    _LOCAL_EDGES = {"HAS_ELEMENT", "CHILD_ELEMENT", "DEFINES", "HAS_MODULE"}
    edge_map: dict[str, list[tuple[str, str, str]]] = {}
    for e in edges:
        if e[2] in _LOCAL_EDGES:
            edge_map.setdefault(e[0], []).append(e)
    seen = set()
    frontier = [obj_id]
    while frontier:
        nxt = []
        for src in frontier:
            for e in edge_map.get(src, []):
                if e not in seen:
                    seen.add(e)
                    keep_ids.add(e[1])
                    nxt.append(e[1])
        frontier = nxt

    nodes2 = [n for n in nodes if n["id"] in keep_ids]
    edges2 = [e for e in edges if e in seen]
    ids = {n["id"] for n in nodes2}
    hashes2 = {k: v for k, v in hashes.items() if k in ids}
    embed_items2 = [it for it in embed_items if it.node_id in ids]
    obj_node = next((n for n in nodes2 if n["id"] == obj_id), None)
    sk = (obj_node or {}).get("props", {}).get("source_key", "")
    checks2 = {sk: object_checksums[sk]} if sk in object_checksums else {}
    return nodes2, edges2, hashes2, embed_items2, checks2


def ensure_collection(client, cfg: AppConfig) -> None:
    from qdrant_client import models

    dim = cfg.embedder.dimensions
    if not client.collection_exists(cfg.qdrant_collection):
        client.create_collection(
            collection_name=cfg.qdrant_collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )
        log.info("created Qdrant collection %s (dim=%d)", cfg.qdrant_collection, dim)


def _payload_for(cfg: AppConfig, n: dict) -> dict | None:
    p = n.get("props") or {}
    kind = n["kind"]
    if kind == "object":
        payload = {"kind": "object", "name": n["name"], "type": n["type"],
                   "synonym": p.get("synonym", ""), "comment": p.get("comment", ""),
                   "is_legacy": bool(p.get("is_legacy"))}
        if p.get("source_key"):
            payload["source_key"] = p["source_key"]
    elif kind == "element":
        payload = {"kind": "element", "name": n["name"], "object": n["object_name"],
                   "type": n["type"], "data_type": p.get("data_type", ""),
                   "comment": p.get("comment", ""), "parent": p.get("parent", "")}
    elif kind == "symbol":
        payload = {"kind": "symbol", "name": n["name"], "object": n["object_name"],
                   "visibility": p.get("visibility", ""), "line": p.get("line", ""),
                   "module": p.get("module", "")}
    else:
        return None
    if cfg.node_id_in_payload:
        payload["node_id"] = n["id"]
    return payload


def _update_payloads_only(cfg: AppConfig, graph: GraphStore, nodes: list[dict]) -> dict:
    """Re-push Qdrant payloads for the given nodes without re-embedding.

    Retrieves the existing vectors and re-upserts them with the fresh payload,
    so no embedding work is done (the ~20 min re-embed is skipped).
    """
    from qdrant_client import QdrantClient, models

    client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None, timeout=60)
    try:
        if not client.collection_exists(cfg.qdrant_collection):
            return {"vectors_updated": 0, "vectors_deleted": 0,
                    "payload_only": True, "note": "collection missing"}

        total = 0
        # Only kinds that carry a Qdrant point; modules have no vector/payload.
        nodes = [n for n in nodes if n["kind"] in ("object", "element", "symbol")]
        for i in range(0, len(nodes), 512):
            chunk = nodes[i : i + 512]
            ids_chunk = [n["id"] for n in chunk]
            recs = client.retrieve(
                collection_name=cfg.qdrant_collection,
                ids=ids_chunk,
                with_vectors=True,
            )
            by_id = {r.id: r for r in recs}
            # Align to retrieved records: a point may be missing after a partial
            # run; skip it rather than upserting a null vector.
            ids = []
            vectors = []
            payloads = []
            for n in chunk:
                r = by_id.get(n["id"])
                if r is None or r.vector is None:
                    continue
                pl = _payload_for(cfg, n)
                if pl is None:
                    continue
                ids.append(n["id"])
                vectors.append(r.vector)
                payloads.append(pl)
            if not ids:
                continue
            batch = models.Batch(ids=ids, vectors=vectors, payloads=payloads)
            client.upsert(collection_name=cfg.qdrant_collection, points=batch, wait=True)
            total += len(ids)
        return {"vectors_updated": total, "vectors_deleted": 0, "payload_only": True}
    finally:
        client.close()


def run_index(cfg: AppConfig, full: bool = False, no_vectors: bool = False,
              object_filter: str | None = None, kind_filter: str | None = None) -> dict:
    from qdrant_client import QdrantClient, models

    report_path = cfg.resolve_txt_root() / "ОтчетПоКонфигурации.txt"
    graph = GraphStore(cfg.graph_db_path)

    # Checksum gate: skip if report unchanged and not --full and not targeted.
    # payload_only and kind/object filters always run (they do targeted work).
    if not full and not cfg.payload_only and object_filter is None and kind_filter is None \
            and report_path.is_file():
        current_sum = _file_checksum(report_path)
        stored_sum = graph.get_meta("config_checksum")
        if stored_sum == current_sum:
            log.info("report unchanged (checksum match), skipping rebuild")
            graph.close()
            return {"skipped": True, "reason": "checksum_match"}

    nodes, edges, hashes, embed_items, n_objects, n_bsl, flags, object_checksums = collect_graph(cfg)

    if object_filter:
        nodes, edges, hashes, embed_items, object_checksums = _filter_object(
            nodes, edges, hashes, embed_items, object_checksums, object_filter
        )
        if not nodes:
            log.warning("--object %r matched no objects", object_filter)

    if kind_filter:
        embed_items = [it for it in embed_items if it.payload.get("kind") == kind_filter]
        if not embed_items:
            log.warning("--kind %r matched no embed items", kind_filter)

    if cfg.payload_only:
        stats = _update_payloads_only(cfg, graph, nodes)
        graph.set_object_checksums(object_checksums)
        graph.close()
        return stats

    # A kind filter embeds only that layer; the graph is already complete in
    # SQLite (built by a prior full/--no-vectors run), so we skip graph writes
    # and stale-node sweep to avoid touching other layers.
    embed_only = kind_filter is not None and object_filter is None

    if embed_only:
        prev_hashes = {}
    else:
        prev_hashes = {} if full else graph.get_node_hashes()

    changed = [it for it in embed_items if prev_hashes.get(it.node_id) != hashes.get(it.node_id)]
    log.info(
        "graph: %d nodes, %d edges | vectors: %d total, %d to update (full=%s)",
        len(nodes), len(edges), len(embed_items), len(changed), full,
    )

    if not embed_only:
        graph.upsert_nodes(nodes)
        graph.upsert_edges(edges)
        if object_filter:
            # Targeted reindex: only update/delete the object's own nodes, never
            # sweep the whole graph (would drop every unrelated object).
            dropped = []
        else:
            keep_ids = {n["id"] for n in nodes} | set(hashes.keys())
            dropped = graph.delete_stale_nodes(keep_ids)
    else:
        dropped = []

    stats = {"objects": n_objects, "bsl_files": n_bsl, "nodes": len(nodes),
             "edges": len(edges), "vectors_total": len(embed_items),
             "vectors_updated": 0, "vectors_deleted": 0}

    if not no_vectors and (changed or dropped):
        client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None)
        ensure_collection(client, cfg)

        if dropped:
            client.delete(
                collection_name=cfg.qdrant_collection,
                points_selector=models.PointIdsList(points=list(dropped)),
            )
            stats["vectors_deleted"] = len(dropped)
            log.info("deleted %d stale points from Qdrant", len(dropped))

        if changed:
            import time
            embedder = Embedder(cfg.embedder)
            batch = 64
            total = len(changed)
            done = 0
            t0 = time.monotonic()
            for i in range(0, total, batch):
                chunk = changed[i : i + batch]
                vectors = embedder.embed_batched([it.text for it in chunk])
                points = [
                    models.PointStruct(id=it.node_id, vector=v, payload=it.payload)
                    for it, v in zip(chunk, vectors)
                ]
                client.upsert(collection_name=cfg.qdrant_collection, points=points, wait=True)
                # Mark this batch as embedded only after a successful upsert,
                # so an interrupted run resumes at the right place next time.
                graph.set_node_hashes({it.node_id: hashes[it.node_id] for it in chunk})
                done += len(points)
                stats["vectors_updated"] = done
                elapsed = time.monotonic() - t0
                vps = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / vps if vps > 0 else 0
                pct = done * 100 // total
                bar_len = 40
                filled = bar_len * done // total
                bar = "█" * filled + "░" * (bar_len - filled)
                print(f"\r  [{bar}] {pct:3d}%  {done}/{total}  {vps:.1f} vec/s  ETA {eta:.0f}s", end="", flush=True)
            print()
            embedder.close()
        client.close()

    if no_vectors:
        # No vectors produced: still record hashes so a later vector run is
        # aware of the current content (otherwise it would re-embed everything).
        graph.set_node_hashes(hashes)

    # Persist object checksums only after a successful build (metadata + bsl).
    graph.set_object_checksums(object_checksums)

    if report_path.is_file():
        graph.set_meta("config_checksum", _file_checksum(report_path))

    graph.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index 1C configuration sources")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--full", action="store_true", help="full rebuild (ignore stored hashes)")
    parser.add_argument("--no-vectors", action="store_true", help="skip embedding step")
    parser.add_argument("--object", default=None,
                        help="reindex a single object (Russian or English name, e.g. Справочник.Колледжи or Catalog.Колледжи)")
    parser.add_argument("--payload-only", action="store_true",
                        help="update Qdrant payloads only (no re-embedding); see ONEC_PAYLOAD_ONLY")
    parser.add_argument("--kind", default=None,
                        help="embed only one layer: object | element | symbol (graph stays in SQLite)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.payload_only:
        cfg.payload_only = True
    stats = run_index(cfg, full=args.full, no_vectors=args.no_vectors,
                      object_filter=args.object, kind_filter=args.kind)
    print("Index complete:", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
