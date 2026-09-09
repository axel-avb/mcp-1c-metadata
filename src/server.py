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
        """List configuration objects (catalogs, documents, registers, ...).

        Args:
            type: optional filter — one of: catalog, document, accumulation_register,
                  information_register, journal_register, calculation_register, constant,
                  enumeration, chart_of_characteristics, exchange_plan, chart_of_accounts.
        Returns:
            Objects grouped by type with element counts.
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
        """Show all elements (attributes, tabular sections, commands, ...) of one object.

        Args:
            object_name: full name like "Каталог.Номенклатура" or short "Номенклатура".
            include_children: also list nested elements (e.g. columns of a tabular section).
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

    @mcp.tool
    def search_config(query: str, top_k: int = 5, kind: str = "") -> str:
        """Semantic search across the configuration: objects, elements and BSL procedures.

        Pipeline: query -> external embedder -> Qdrant ANN (top_k x multiplier)
                  -> external reranker -> top results.

        Args:
            query: natural-language query in Russian or English.
            top_k: number of results to return (min 1).
            kind: optional filter — "object" | "element" | "symbol".
        """
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
