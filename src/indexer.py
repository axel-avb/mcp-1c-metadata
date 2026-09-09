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

from .bs_parser import parse_bs_file, infer_module_role
from .config import AppConfig, load_config
from .config_parser import (
    ConfigElement,
    ConfigObject,
    element_text_repr,
    object_text_repr,
    parse_config_root,
    stable_id,
)
from .embedder import Embedder
from .graph import GraphStore

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


def collect_graph(cfg: AppConfig):
    """Parse XML + BSL. Returns (nodes, edges, hashes, embedding_items, n_objects, n_bsl)."""
    objects = parse_config_root(cfg.config_root)
    log.info("parsed %d configuration objects from %s", len(objects), cfg.config_root)

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
        text = element_text_repr(obj, elem)
        nodes.append({
            "id": eid, "kind": "element", "name": elem.name, "object_name": obj.name,
            "type": elem.elem_type,
            "props": {"synonym": elem.synonym, "comment": elem.comment,
                      "data_type": elem.data_type, "ref_object": elem.ref_object},
        })
        hashes[eid] = _hash("element", text)
        embed_items.append(EmbeddingItem(
            eid, text,
            payload={"kind": "element", "name": elem.name, "object": obj.name,
                     "type": elem.elem_type, "data_type": elem.data_type,
                     "comment": elem.comment},
        ))
        edges.append((parent_id, eid, edge_kind))
        if elem.ref_object and elem.ref_object in obj_by_name:
            edges.append((eid, obj_by_name[elem.ref_object], "REFERENCE"))
        for child in elem.children:
            add_element(obj, child, eid, "CHILD_ELEMENT", f"{path}/{elem.name}")

    for obj in objects:
        oid = obj_by_name[obj.name]
        text = object_text_repr(obj)
        nodes.append({
            "id": oid, "kind": "object", "name": obj.name, "object_name": obj.name,
            "type": obj.type,
            "props": {"synonym": obj.synonym, "comment": obj.comment},
        })
        hashes[oid] = _hash("object", text)
        embed_items.append(EmbeddingItem(
            oid, text,
            payload={"kind": "object", "name": obj.name, "type": obj.type,
                     "synonym": obj.synonym, "comment": obj.comment},
        ))
        for elem in obj.elements:
            add_element(obj, elem, oid, "HAS_ELEMENT", "")

    # ---- BSL modules, symbols, CALLS / USES --------------------------------
    bs_files: list[Path] = sorted(cfg.config_root.rglob("*.bsl")) or sorted(cfg.config_root.rglob("*.bs"))
    log.info("found %d BSL files", len(bs_files))

    symbol_ids_by_name: dict[str, list[str]] = {}
    parsed_modules = []  # (module_path, module) for the CALLS pass

    for f in bs_files:
        role, object_name = infer_module_role(f.parent, f.name)
        owner_obj_id = obj_by_name.get(object_name, "")
        module = parse_bs_file(f, module_role=role, object_name=object_name)
        parsed_modules.append((f, module))

        rel = str(f.relative_to(cfg.config_root))
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
            embed_items.append(EmbeddingItem(
                sid,
                f"{sym.kind} {sym.name}({sym.params}) — {object_name}, модуль: {role}\n{sym.body}",
                payload={"kind": "symbol", "name": sym.name, "object": object_name,
                         "visibility": sym.visibility, "line": sym.line, "module": rel},
            ))
            edges.append((mid, sid, "DEFINES"))

        for prefix, short in module.object_refs:
            for target in obj_by_short.get(short, []):
                edges.append((mid, target, "USES"))
        if owner_obj_id:
            edges.append((owner_obj_id, mid, "HAS_MODULE"))

    # CALLS edges (symbol -> symbol, resolved by name in the global namespace)
    seen_calls: set[tuple[str, str]] = set()
    for f, module in parsed_modules:
        rel = str(f.relative_to(cfg.config_root))
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

    return nodes, edges, hashes, embed_items, len(objects), len(bs_files)


def ensure_collection(client, cfg: AppConfig) -> None:
    from qdrant_client import models

    dim = cfg.embedder.dimensions
    if not client.collection_exists(cfg.qdrant_collection):
        client.create_collection(
            collection_name=cfg.qdrant_collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )
        log.info("created Qdrant collection %s (dim=%d)", cfg.qdrant_collection, dim)


def run_index(cfg: AppConfig, full: bool = False, no_vectors: bool = False) -> dict:
    from qdrant_client import QdrantClient, models

    nodes, edges, hashes, embed_items, n_objects, n_bsl = collect_graph(cfg)

    graph = GraphStore(cfg.graph_db_path)
    prev_hashes = {} if full else graph.get_node_hashes()
    changed = [it for it in embed_items if prev_hashes.get(it.node_id) != hashes.get(it.node_id)]
    log.info(
        "graph: %d nodes, %d edges | vectors: %d total, %d to update (full=%s)",
        len(nodes), len(edges), len(embed_items), len(changed), full,
    )

    graph.upsert_nodes(nodes)
    graph.upsert_edges(edges)
    keep_ids = {n["id"] for n in nodes} | set(hashes.keys())
    dropped = graph.delete_stale_nodes(keep_ids)
    graph.set_node_hashes(hashes)

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
            embedder = Embedder(cfg.embedder)
            batch = 64
            for i in range(0, len(changed), batch):
                chunk = changed[i : i + batch]
                vectors = embedder.embed_batched([it.text for it in chunk])
                points = [
                    models.PointStruct(id=it.node_id, vector=v, payload=it.payload)
                    for it, v in zip(chunk, vectors)
                ]
                client.upsert(collection_name=cfg.qdrant_collection, points=points, wait=True)
                stats["vectors_updated"] += len(points)
                log.info("embedded %d/%d", min(i + batch, len(changed)), len(changed))
            embedder.close()
        client.close()

    graph.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index 1C configuration sources")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--full", action="store_true", help="full rebuild (ignore stored hashes)")
    parser.add_argument("--no-vectors", action="store_true", help="skip embedding step")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    stats = run_index(cfg, full=args.full, no_vectors=args.no_vectors)
    print("Index complete:", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
