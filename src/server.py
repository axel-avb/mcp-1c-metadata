"""1C Configuration MCP Server.

Exposes the parsed 1C configuration (XML export + BSL code) as MCP tools:
  - list_objects          top-level objects by type with element counts
  - get_object_elements   elements of one object (attributes, commands, ...)
  - search_config         semantic search: embed -> Qdrant -> rerank
  - get_symbol            procedure/function details + body
  - get_callers           who calls this procedure (call graph)
  - get_callees           what this procedure calls
  - get_references        references to/from a configuration object
  - reindex               rebuild the index on demand

Transport: streamable HTTP (FastMCP 4.x).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastmcp import FastMCP

from .config import AppConfig, load_config
from .embedder import Embedder
from .graph import GraphStore
from .reranker import Reranker

log = logging.getLogger(__name__)

_TYPE_LABELS = {
    "catalog": "Каталоги",
    "document": "Документы",
    "accumulation_register": "Регистры накопления",
    "information_register": "Регистры сведений",
    "journal_register": "Регистры бухгалтерии",
    "calculation_register": "Регистры расчётов",
    "constant": "Константы",
    "enumeration": "Перечисления",
    "chart_of_characteristics": "Планы видов характеристик",
    "exchange_plan": "Планы обмена",
    "chart_of_accounts": "Планы счетов",
    "subsystem": "Подсистемы",
    "library": "Библиотеки",
}


class App:
    """Holds shared state (graph store, embedder, reranker, config)."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.graph = GraphStore(cfg.graph_db_path)
        self.embedder = Embedder(cfg.embedder)
        self.reranker = Reranker(cfg.reranker)


class _ReindexState:
    """Thread-safe status for background reindexing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.status = "idle"
        self.stats: dict | None = None
        self.error: str = ""

    def start(self) -> None:
        with self._lock:
            self.running = True
            self.status = "running"
            self.stats = None
            self.error = ""

    def finish(self, stats: dict | None, error: str) -> None:
        with self._lock:
            self.running = False
            self.status = "failed" if error else "done"
            self.stats = stats
            self.error = error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "status": self.status,
                "stats": self.stats,
                "error": self.error,
            }


def _fmt_node(n: dict[str, Any], with_body: bool = False) -> str:
    p = n.get("props") or {}
    lines = [f"{n.get('kind', '')} {n.get('name', '')}"]
    if n.get("object_name"):
        lines.append(f"  объект: {n['object_name']}")
    if p.get("synonym"):
        lines.append(f"  синоним: {p['synonym']}")
    if p.get("comment"):
        lines.append(f"  назначение: {p['comment']}")
    if p.get("data_type"):
        lines.append(f"  тип данных: {p['data_type']}")
    if p.get("ref_object"):
        lines.append(f"  ссылка на: {p['ref_object']}")
    if p.get("visibility"):
        lines.append(f"  видимость: {p['visibility']}")
    if p.get("line"):
        lines.append(f"  строка: {p['line']}")
    if with_body and p.get("body"):
        lines.append(p["body"])
    return "\n".join(lines)


def build_server(cfg: AppConfig | None = None) -> FastMCP:
    cfg = cfg or load_config()
    app = App(cfg)
    reindex_state = _ReindexState()

    auth = None
    if cfg.auth_token:
        from fastmcp.server.auth import StaticTokenVerifier

        auth = StaticTokenVerifier(
            tokens={cfg.auth_token: {"client_id": "onec", "scopes": []}}
        )

    mcp = FastMCP(
        name="1c-configuration",
        auth=auth,
        instructions=(
            "MCP server for browsing a 1C:Enterprise configuration exported to XML/BSL sources. "
            "Use list_objects to see the object tree, get_object_elements to drill into an object, "
            "search_config for semantic search across metadata and code, and the call-graph tools "
            "(get_callers/get_callees) to navigate BSL procedures."
        ),
    )

    # ------------------------------------------------------------------ tools

    @mcp.tool
    def list_objects(type: str = "") -> str:
        """List configuration objects grouped by type with element counts.

        Args:
            type: optional object type filter (empty = all).
        """
        objects = app.graph.objects(type or None)
        if not objects:
            return "No objects found. Run the indexer first (python -m src.indexer)."

        groups: dict[str, list[dict[str, Any]]] = {}
        for o in objects:
            groups.setdefault(o.get("type", "?"), []).append(o)

        out: list[str] = []
        for t in sorted(groups):
            label = _TYPE_LABELS.get(t, t)
            items = groups[t]
            out.append(f"{label} ({t}) — {len(items)}")
            for o in items:
                p = o.get("props") or {}
                elems = len(app.graph.elements_of(o["id"]))
                suffix = f" — {p['synonym']}" if p.get("synonym") and p["synonym"] != o["name"] else ""
                out.append(f"  • {o['name']}{suffix} [{elems} elem.]")
        return "\n".join(out)

    @mcp.tool
    def get_object_elements(object_name: str, include_children: bool = True) -> str:
        """Show all elements of one object.

        Args:
            object_name: full or short object name.
            include_children: also list nested elements (e.g. table columns).
        """
        obj = app.graph.object_by_name(object_name)
        if obj is None:
            # try short-name match
            cands = [o for o in app.graph.objects() if o["name"].endswith("." + object_name)]
            if not cands:
                return f"Object not found: {object_name}"
            obj = cands[0]

        lines = [_fmt_node(obj), ""]
        elems = app.graph.elements_of(obj["id"])
        for e in elems:
            p = e.get("props") or {}
            header = f"- {e.get('type', 'Элемент')}: {e['name']}"
            if p.get("data_type"):
                header += f" [{p['data_type']}]"
            if p.get("ref_object"):
                header += f" -> {p['ref_object']}"
            lines.append(header)
            if p.get("comment"):
                lines.append(f"    назначение: {p['comment']}")
            if include_children:
                for c in _children(app.graph, e["id"]):
                    cp = c.get("props") or {}
                    ch = f"  - {c.get('type', 'Элемент')}: {c['name']}"
                    if cp.get("data_type"):
                        ch += f" [{cp['data_type']}]"
                    lines.append(ch)
        return "\n".join(lines)

    _VALID_KINDS = {"object", "element", "symbol"}

    def _search_config(query: str, top_k: int = 5, kind: str = "") -> str:
        top_k = max(1, top_k)
        if kind and kind not in _VALID_KINDS:
            return f"Invalid kind: {kind!r}. Use one of: {', '.join(sorted(_VALID_KINDS))}."

        candidates = top_k * cfg.search.candidate_multiplier
        qv = app.embedder.embed_one(query)

        from qdrant_client import QdrantClient, models

        client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None)
        try:
            if not client.collection_exists(cfg.qdrant_collection):
                return "Vector index is empty. Run the indexer first (python -m src.indexer)."

            must = [models.FieldCondition(key="kind", match=models.MatchValue(value=kind))] if kind else []
            res = client.query_points(
                collection_name=cfg.qdrant_collection,
                query=qv,
                limit=candidates,
                query_filter=models.Filter(must=must) if must else None,
                with_payload=True,
            )
            hits = [p for p in res.points if p.payload]
            if not hits:
                return "No results."

            docs = [
                f"{h.payload.get('kind', '')} {h.payload.get('name', '')}"
                + (f" — {h.payload['object']}" if h.payload.get("object") else "")
                + (f"\n{h.payload['comment']}" if h.payload.get("comment") else "")
                for h in hits
            ]
            ranked = app.reranker.rerank(query, docs, top_n=top_k)

            out: list[str] = []
            for r in ranked:
                h = hits[r.index]
                p = h.payload
                score = f"{r.score:.3f}" if r.score else "n/a"
                line = f"[{score}] {p.get('kind', '?')}: {p.get('name', '?')}"
                extra = []
                if p.get("object"):
                    extra.append(f"объект: {p['object']}")
                if p.get("parent"):
                    extra.append(f"в: {p['parent']}")
                if p.get("type"):
                    extra.append(str(p["type"]))
                if p.get("data_type"):
                    extra.append(p["data_type"])
                if p.get("module"):
                    extra.append(f"файл: {p['module']}")
                if p.get("line"):
                    extra.append(f"строка: {p['line']}")
                if extra:
                    line += " (" + ", ".join(extra) + ")"
                if p.get("comment"):
                    line += f"\n    назначение: {p['comment']}"
                if p.get("type") == "tabular_section" and p.get("object"):
                    cols = None
                    if p.get("node_id"):
                        cols = _children(app.graph, p["node_id"])
                    else:
                        node = app.graph.element_by(p["object"], p["name"])
                        cols = _children(app.graph, node["id"]) if node else []
                    if cols:
                        def _col(c):
                            dt = (c.get("props") or {}).get("data_type", "")
                            return f"{c['name']} [{dt}]" if dt else c["name"]
                        line += "\n    колонки: " + ", ".join(_col(c) for c in cols)
                out.append(line)
            return "\n".join(out)
        finally:
            client.close()

    @mcp.tool
    def search_config(query: str, top_k: int = 5, kind: str = "") -> str:
        """Semantic search across objects, elements and BSL procedures.

        Args:
            query: natural-language query in Russian or English.
            top_k: number of results to return (min 1).
            kind: optional filter — "object" | "element" | "symbol".
        """
        return _search_config(query, top_k=top_k, kind=kind)

    @mcp.tool
    def search_bsl_code(query: str, top_k: int = 5) -> str:
        """Semantic search over BSL routine bodies (procedures/functions).

        Args:
            query: natural-language phrase describing what the code does.
            top_k: number of results to return (min 1).
        """
        return _search_config(query, top_k=top_k, kind="symbol")

    @mcp.tool
    def get_symbol(name: str, object_name: str = "") -> str:
        """Show a BSL procedure/function: signature, visibility, module, body.

        Args:
            name: procedure name (global namespace).
            object_name: optional disambiguation — which configuration object's module.
        """
        syms = app.graph.symbols_by_name(name)
        if object_name:
            syms = [s for s in syms if object_name in s.get("object_name", "")]
        if not syms:
            return f"Symbol not found: {name}"
        out: list[str] = []
        for s in syms:
            p = s.get("props") or {}
            body = p.get("body", "")
            out.append(
                f"{s.get('type', 'procedure')} {s['name']}({p.get('params', '')})\n"
                f"  объект: {s.get('object_name', '?')}\n"
                f"  модуль: {p.get('module', '?')} (строка {p.get('line', '?')})\n"
                f"  видимость: {p.get('visibility', '?')}"
                + (f"\n\n{body}" if body else "")
            )
        return "\n\n".join(out)

    @mcp.tool
    def get_callers(name: str, object_name: str = "") -> str:
        """List procedures that call the given procedure (call graph, inbound).

        Args:
            name: callee procedure name.
            object_name: optional disambiguation of which overload to use.
        """
        syms = app.graph.symbols_by_name(name)
        if object_name:
            syms = [s for s in syms if object_name in s.get("object_name", "")]
        if not syms:
            return f"Symbol not found: {name}"
        callers = app.graph.callers([s["id"] for s in syms])
        if not callers:
            return f"No callers found for {name}."
        lines = [f"Callers of {name}:"]
        for c in callers:
            p = c.get("props") or {}
            lines.append(f"  • {c['name']} — {c.get('object_name', '?')} ({p.get('module', '?')})")
        return "\n".join(lines)

    @mcp.tool
    def get_callees(name: str, object_name: str = "") -> str:
        """List procedures called by the given procedure (call graph, outbound).

        Args:
            name: caller procedure name.
            object_name: optional disambiguation of which overload to use.
        """
        syms = app.graph.symbols_by_name(name)
        if object_name:
            syms = [s for s in syms if object_name in s.get("object_name", "")]
        if not syms:
            return f"Symbol not found: {name}"
        callees = app.graph.callees([s["id"] for s in syms])
        if not callees:
            return f"No callees found for {name}."
        lines = [f"Callees of {name}:"]
        for c in callees:
            p = c.get("props") or {}
            lines.append(f"  • {c['name']} — {c.get('object_name', '?')} ({p.get('module', '?')})")
        return "\n".join(lines)

    @mcp.tool
    def get_references(object_name: str, direction: str = "both") -> str:
        """Show configuration references for an object.

        Args:
            object_name: full or short object name.
            direction: "in" (who references it), "out" (what it references), "both".
        """
        obj = app.graph.object_by_name(object_name)
        if obj is None:
            cands = [o for o in app.graph.objects() if o["name"].endswith("." + object_name)]
            if not cands:
                return f"Object not found: {object_name}"
            obj = cands[0]

        out: list[str] = [_fmt_node(obj), ""]
        if direction in ("in", "both"):
            refs_in = app.graph.references_to(obj["id"])
            out.append(f"Ссылки НА этот объект ({len(refs_in)}):")
            for r in refs_in:
                p = r.get("props") or {}
                out.append(f"  • {r['name']} [{r.get('type', '')}] — {r.get('object_name', '?')}"
                           + (f" ({p['data_type']})" if p.get("data_type") else ""))
        if direction in ("out", "both"):
            refs_out = app.graph.references_from(obj["id"])
            out.append(f"\nСсылки С этого объекта ({len(refs_out)}):")
            for r in refs_out:
                out.append(f"  • {r['name']}")
        return "\n".join(out)

    @mcp.tool
    def reindex(full: bool = False) -> str:
        """Rebuild the index from the configuration sources in the background.

        The rebuild runs in a background thread so a full re-embed of a large
        configuration does not block the MCP request. Poll `reindex_status`
        for progress and completion.

        Args:
            full: full rebuild; otherwise incremental (only changed files).
        """
        if reindex_state.running:
            return "Reindex already in progress. Poll reindex_status for completion."

        from .indexer import run_index

        reindex_state.start()

        def worker() -> None:
            stats = None
            error = ""
            try:
                stats = run_index(cfg, full=full)
            except Exception as e:  # noqa: BLE001 — surface failure via status
                error = f"{type(e).__name__}: {e}"
            reindex_state.finish(stats, error)

        threading.Thread(target=worker, daemon=True).start()
        return "Reindex started in background. Poll reindex_status for completion."

    @mcp.tool
    def reindex_status() -> str:
        """Show the status of the background reindex job."""
        s = reindex_state.snapshot()
        if s["status"] == "idle":
            return "No reindex has been run yet."
        if s["status"] == "running":
            return "Reindex in progress..."
        if s["status"] == "failed":
            return f"Reindex failed: {s['error']}"
        return f"Reindex complete: {s['stats']}"

    @mcp.tool
    def graph_stats() -> str:
        """Show index statistics: node/edge counts by kind."""
        s = app.graph.stats()
        if not s:
            return "Index is empty. Run reindex first."
        return "\n".join(f"{k}: {v}" for k, v in sorted(s.items()))

    # -------------------------------------------------- Tier A: metadata ---

    def _resolve_object(object_ref: str) -> dict[str, Any] | None:
        obj = app.graph.object_by_name(object_ref)
        if obj is not None:
            return obj
        cands = [
            o for o in app.graph.objects()
            if o["name"].endswith("." + object_ref)
            or (o.get("props") or {}).get("source_key") == object_ref
        ]
        return cands[0] if cands else None

    @mcp.tool
    def get_metadata(mode: str = "summary", category: str = "",
                     object_name: str = "", object_match: str = "contains",
                     limit: int = 0, offset: int = 0) -> str:
        """Inventory of the configuration.

        Args:
            mode: summary (counts) | categories (by type) | objects (list).
            category: optional object type filter.
            object_name: name pattern for mode=objects.
            object_match: matching mode.
            limit: page size (0 = all).
            offset: page offset.
        """
        if mode == "categories":
            counts = app.graph.object_counts()
            if not counts:
                return "Index is empty. Run reindex first."
            return "\n".join(
                f"{_TYPE_LABELS.get(t, t)} ({t}) — {c}" for t, c in sorted(counts.items())
            )
        if mode == "objects":
            if object_name:
                objs = app.graph.objects_matching(
                    object_name, object_match, category or None, limit, offset
                )
            else:
                objs = app.graph.objects(category or None)
                if offset:
                    objs = objs[offset:]
                if limit:
                    objs = objs[:limit]
            if not objs:
                return "No objects found."
            return "\n".join(f"{o['name']} [{o.get('type', '')}]" for o in objs)
        s = app.graph.stats()
        if not s:
            return "Index is empty. Run reindex first."
        cats = app.graph.object_counts()
        return "\n".join([
            f"objects: {s.get('object', 0)}",
            f"elements: {s.get('element', 0)}",
            f"symbols: {s.get('symbol', 0)}",
            f"modules: {s.get('module', 0)}",
            f"categories: {len(cats)}",
        ])

    _SECTION_TYPES = {
        "attributes": {"attribute"},
        "tabular_parts": {"tabular_section"},
        "characteristics": {"characteristic", "characteristic_type"},
        "resources": {"resource"},
        "dimensions": {"dimension"},
        "forms": {"form"},
        "commands": {"command"},
        "layouts": {"layout", "template"},
        "enum_values": {"value"},
        "predefined": {"predefined_data"},
        "url_templates": {"operation"},
        "url_methods": {"parameter"},
    }

    @mcp.tool
    def get_metadata_object_structure(object_ref: str, sections: str = "",
                                      tabular_part: str = "") -> str:
        """Structure of one object grouped by section.

        Args:
            object_ref: object name (full/short/source_key).
            sections: comma-separated section names (empty = all).
            tabular_part: parent tabular-section name for tabular_attributes.
        """
        obj = _resolve_object(object_ref)
        if obj is None:
            return f"Object not found: {object_ref}"
        elems = app.graph.elements_of(obj["id"])
        want = [s.strip() for s in sections.split(",") if s.strip()] or \
            ["overview", *_SECTION_TYPES.keys()]
        out = [_fmt_node(obj), ""]

        def render(nodes):
            for e in nodes:
                p = e.get("props") or {}
                line = f"  - {e.get('type', '')}: {e['name']}"
                if p.get("data_type"):
                    line += f" [{p['data_type']}]"
                if p.get("ref_object"):
                    line += f" -> {p['ref_object']}"
                out.append(line)

        for sec in want:
            if sec == "overview":
                out.append(f"overview: {len(elems)} direct elements")
            elif sec == "tabular_attributes":
                if not tabular_part:
                    out.append("tabular_attributes: pass tabular_part")
                    continue
                node = app.graph.element_by(obj["name"], tabular_part)
                cols = _children(app.graph, node["id"]) if node else []
                out.append(f"tabular_attributes ({tabular_part}) — {len(cols)}:")
                render(cols)
            elif sec in _SECTION_TYPES:
                matched = [e for e in elems if e.get("type") in _SECTION_TYPES[sec]]
                out.append(f"{sec} — {len(matched)}:")
                render(matched)
            elif sec == "default_forms":
                forms = [e for e in elems if e.get("type") == "form"]
                out.append(f"default_forms — {len(forms)}:")
                render(forms)
            else:
                out.append(f"{sec}: not available in the index")
        return "\n".join(out)

    @mcp.tool
    def get_metadata_element_type(object_ref: str, element_type: str,
                                  container_ref: str = "") -> str:
        """Typed children of an object.

        Args:
            object_ref: object name.
            element_type: comma-separated element types to include.
            container_ref: parent element for nested children (e.g. tabular part).
        """
        obj = _resolve_object(object_ref)
        if obj is None:
            return f"Object not found: {object_ref}"
        types = {t.strip() for t in element_type.split(",") if t.strip()}
        if container_ref:
            node = app.graph.element_by(obj["name"], container_ref)
            nodes = _children(app.graph, node["id"]) if node else []
        else:
            nodes = app.graph.elements_of(obj["id"])
        matched = [e for e in nodes if e.get("type") in types]
        if not matched:
            return "No elements of the requested type(s)."
        out = []
        for e in matched:
            p = e.get("props") or {}
            line = f"- {e.get('type', '')}: {e['name']}"
            if p.get("data_type"):
                line += f" [{p['data_type']}]"
            if p.get("ref_object"):
                line += f" -> {p['ref_object']}"
            out.append(line)
        return "\n".join(out)

    @mcp.tool
    def get_metadata_details(ref_type: str, ref: str, owner_ref: str = "",
                             mode: str = "resolve") -> str:
        """Resolve a reference to a node card.

        Args:
            ref_type: node kind (object/element/symbol).
            ref: the name to resolve.
            owner_ref: owning object name (disambiguation).
            mode: resolve (card) | properties (with raw props).
        """
        node = None
        if ref_type == "object":
            node = _resolve_object(ref)
        elif ref_type in ("symbol", "routine"):
            syms = app.graph.symbols_by_name(ref)
            if owner_ref:
                syms = [s for s in syms if owner_ref in s.get("object_name", "")]
            node = syms[0] if syms else None
        else:
            obj = _resolve_object(owner_ref) if owner_ref else None
            if obj is not None:
                node = app.graph.element_by(obj["name"], ref)
                if node is None:
                    node = next((e for e in app.graph.elements_of(obj["id"])
                                 if e["name"] == ref), None)
        if node is None:
            return f"Not found: {ref_type} {ref!r}"
        if mode == "properties":
            return _fmt_node(node) + "\nprops: " + str(node.get("props") or {})
        return _fmt_node(node)

    @mcp.tool
    def inspect_metadata_object(object_ref: str, detail: str = "standard",
                                sections: str = "") -> str:
        """Object dossier: counts + bounded lists by section.

        Args:
            object_ref: object name.
            detail: list cap (brief/standard/extended).
            sections: comma-separated sections (empty = all).
        """
        obj = _resolve_object(object_ref)
        if obj is None:
            return f"Object not found: {object_ref}"
        cap = {"brief": 5, "standard": 15, "extended": 50}.get(detail, 15)
        elems = app.graph.elements_of(obj["id"])
        want = [s.strip() for s in sections.split(",") if s.strip()] or \
            ["overview", "structure", "forms", "bsl", "usages"]
        out = [_fmt_node(obj), ""]
        from collections import Counter
        counts = Counter(e.get("type", "") for e in elems)
        if "overview" in want:
            out.append("overview: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if "structure" in want:
            out.append("structure:")
            for e in elems[:cap]:
                out.append(f"  - {e.get('type', '')}: {e['name']}")
            if len(elems) > cap:
                out.append(f"  ... +{len(elems) - cap}")
        if "forms" in want:
            forms = [e for e in elems if e.get("type") == "form"]
            out.append(f"forms — {len(forms)}: " + ", ".join(e["name"] for e in forms[:cap]))
        if "bsl" in want:
            mods = app.graph.modules_of_object(obj["id"])
            out.append(f"modules — {len(mods)}:")
            for m in mods[:cap]:
                out.append(f"  - {m['name']} ({len(app.graph.symbols_of_module(m['id']))} routines)")
        if "usages" in want:
            refs_in = app.graph.references_to(obj["id"])
            users = app.graph.modules_using(obj["id"])
            out.append(f"usages: referenced by {len(refs_in)} element(s), "
                       f"used by {len(users)} module(s)")
            for r in refs_in[:cap]:
                out.append(f"  • {r['name']} — {r.get('object_name', '?')}")
        return "\n".join(out)

    @mcp.tool
    def find_metadata_objects(search_by: str, search_text: str = "",
                              within_object: str = "", limit: int = 20) -> str:
        """Find objects by description or by a child element name.

        Args:
            search_by: description or a child element type.
            search_text: text to match.
            within_object: restrict to one object.
            limit: max results.
        """
        if search_by == "description":
            hits = []
            for o in app.graph.objects():
                p = o.get("props") or {}
                hay = " ".join((o["name"], p.get("synonym", ""), p.get("comment", "")))
                if search_text.lower() in hay.lower():
                    hits.append(o["name"])
                    if len(hits) >= limit:
                        break
            if within_object:
                hits = [h for h in hits if within_object in h]
            return "\n".join(hits) if hits else "No objects found."
        elem_map = {"attribute": "attribute", "tabular_part": "tabular_section",
                    "resource": "resource", "dimension": "dimension",
                    "form": "form", "command": "command", "layout": "layout"}
        if search_by not in elem_map:
            return f"search_by={search_by!r} not available in the index."
        elems = app.graph.elements_matching(elem_type=elem_map[search_by],
                                            name_pattern=search_text or None)
        seen: list[str] = []
        for e in elems:
            on = e.get("object_name", "")
            if on and on not in seen and (not within_object or within_object in on):
                seen.append(on)
            if len(seen) >= limit:
                break
        return "\n".join(seen) if seen else "No objects found."

    @mcp.tool
    def find_metadata_elements(element_type: str, element_name: str = "",
                               owner_object: str = "", mode: str = "contains",
                               limit: int = 50) -> str:
        """Find child elements across the project.

        Args:
            element_type: element type to match.
            element_name: name pattern.
            owner_object: restrict to one owning object.
            mode: matching mode.
            limit: max results.
        """
        elems = app.graph.elements_matching(
            elem_type=element_type, name_pattern=element_name or None,
            mode=mode, object_name=owner_object or None, limit=limit,
        )
        if not elems:
            return "No elements found."
        return "\n".join(
            f"{e['name']} [{e.get('type', '')}] — {e.get('object_name', '?')}" for e in elems
        )

    @mcp.tool
    def find_metadata_usages(target_ref: str, mode: str = "objects") -> str:
        """Who uses an object (references / modules).

        Args:
            target_ref: object name.
            mode: objects (references + modules). register_movements not available.
        """
        obj = _resolve_object(target_ref)
        if obj is None:
            return f"Object not found: {target_ref}"
        refs = app.graph.references_to(obj["id"])
        mods = app.graph.modules_using(obj["id"])
        out = [f"Usages of {obj['name']}:", f"referenced by {len(refs)} element(s):"]
        for r in refs[:50]:
            out.append(f"  • {r['name']} [{r.get('type', '')}] — {r.get('object_name', '?')}")
        out.append(f"used in {len(mods)} module(s):")
        for m in mods[:50]:
            out.append(f"  • {m['name']}")
        return "\n".join(out)

    # ------------------------------------------------------- Tier A: BSL ---

    @mcp.tool
    def get_bsl_modules(mode: str, owner_ref: str = "", module_ref: str = "",
                        routine_name: str = "") -> str:
        """Modules of an owner and their routines.

        Args:
            mode: modules_of_owner / module_routines / common_module_routines.
            owner_ref: object/owner name.
            module_ref: module name or id.
            routine_name: routine name filter.
        """
        if mode in ("modules_of_owner", "modules_by_owner_name"):
            obj = _resolve_object(owner_ref)
            if obj is None:
                return f"Object not found: {owner_ref}"
            mods = app.graph.modules_of_object(obj["id"])
            if not mods:
                return f"No modules for {obj['name']}."
            return "\n".join(f"{m['name']} ({m.get('type', '')})" for m in mods)
        if mode in ("module_routines", "common_module_routines"):
            mid = module_ref
            if not mid:
                rows = app.graph.conn.execute(
                    "SELECT id FROM nodes WHERE kind='module' AND name LIKE ? LIMIT 1",
                    (f"%{owner_ref}%",),
                ).fetchall()
                if not rows:
                    return f"Module not found: {owner_ref}"
                mid = rows[0]["id"]
            syms = app.graph.symbols_of_module(mid)
            if routine_name:
                syms = [s for s in syms if routine_name in s["name"]]
            if not syms:
                return "No routines."
            return "\n".join(f"{s['name']} ({s.get('type', '')})" for s in syms)
        return f"Unknown mode: {mode}"

    @mcp.tool
    def search_bsl_routines(name: str = "", mode: str = "name",
                            object_name: str = "", exported_only: bool = False,
                            limit: int = 50) -> str:
        """Search BSL routines.

        Args:
            name: name pattern (mode=name) or signature substring (mode=signature).
            mode: name | exported | signature.
            object_name: owner object filter.
            exported_only: keep only Экспорт routines.
            limit: max results.
        """
        if mode == "unused":
            return "mode=unused is not available in the index."
        exported = True if (exported_only or mode == "exported") else None
        syms = app.graph.search_symbols(
            name_pattern=name or None, object_name=object_name or None,
            exported=exported, limit=limit if mode != "signature" else 0,
        )
        if mode == "signature" and name:
            syms = [s for s in syms
                    if name.lower() in ((s.get("props") or {}).get("params", "") or "").lower()]
        syms = syms[:limit]
        if not syms:
            return "No routines found."
        return "\n".join(
            f"{s['name']}({(s.get('props') or {}).get('params', '')}) — "
            f"{s.get('object_name', '?')}"
            + (f" [{(s.get('props') or {}).get('visibility', '')}]"
               if (s.get("props") or {}).get("visibility") else "")
            for s in syms
        )

    @mcp.tool
    def get_bsl_routine_body(routine_ref: str, owner_ref: str = "",
                             body_offset: int = 0, body_limit: int = 4000) -> str:
        """Body and metadata of a routine, with pagination.

        Args:
            routine_ref: routine name.
            owner_ref: optional owner object filter.
            body_offset: character offset into the body.
            body_limit: max characters to return.
        """
        syms = app.graph.symbols_by_name(routine_ref)
        if owner_ref:
            syms = [s for s in syms if owner_ref in s.get("object_name", "")]
        if not syms:
            return f"Symbol not found: {routine_ref}"
        out = []
        for s in syms:
            p = s.get("props") or {}
            body = p.get("body", "") or ""
            chunk = body[body_offset:body_offset + body_limit]
            more = len(body) - (body_offset + len(chunk))
            out.append(
                f"{s.get('type', 'procedure')} {s['name']}({p.get('params', '')})\n"
                f"  объект: {s.get('object_name', '?')}\n"
                f"  модуль: {p.get('module', '?')} (строка {p.get('line', '?')})\n"
                f"  видимость: {p.get('visibility', '?')}\n\n{chunk}"
                + (f"\n... [{more} chars left]" if more > 0 else "")
            )
        return "\n\n".join(out)

    @mcp.tool
    def get_bsl_call_graph(routine_ref: str, mode: str = "callees",
                           depth: int = 2, owner_ref: str = "") -> str:
        """Call graph from a routine.

        Args:
            routine_ref: routine name.
            mode: callees | callers | subtree.
            depth: traversal depth (mode=subtree).
            owner_ref: optional owner object filter.
        """
        syms = app.graph.symbols_by_name(routine_ref)
        if owner_ref:
            syms = [s for s in syms if owner_ref in s.get("object_name", "")]
        if not syms:
            return f"Symbol not found: {routine_ref}"
        ids = [s["id"] for s in syms]
        if mode == "callees":
            nodes = app.graph.callees(ids)
            if not nodes:
                return "No callees."
            return "Callees:\n" + "\n".join(
                f"  • {n['name']} — {n.get('object_name', '?')}" for n in nodes)
        if mode == "callers":
            nodes = app.graph.callers(ids)
            if not nodes:
                return "No callers."
            return "Callers:\n" + "\n".join(
                f"  • {n['name']} — {n.get('object_name', '?')}" for n in nodes)
        pairs = app.graph.call_subtree(ids, direction="out", max_depth=max(1, depth))
        if not pairs:
            return "No calls."
        lines = [f"Subtree of {routine_ref} (depth {depth}):"]
        for d, n in pairs:
            lines.append("  " * d + f"• {n['name']} — {n.get('object_name', '?')}")
        return "\n".join(lines)

    return mcp


def _children(graph: GraphStore, element_id: str) -> list[dict[str, Any]]:
    cur = graph.conn.execute(
        "SELECT n.* FROM nodes n JOIN edges e ON e.dst = n.id "
        "WHERE e.src = ? AND e.kind='CHILD_ELEMENT' ORDER BY n.name",
        (element_id,),
    )
    return GraphStore._rows_to_dicts(cur.fetchall())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    mcp = build_server(cfg)
    mcp.run(transport="streamable-http", host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
