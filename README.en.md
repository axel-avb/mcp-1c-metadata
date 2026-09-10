# 1C Configuration MCP Server

An MCP server for navigating a 1C:Enterprise configuration exported to source
code (XML metadata + BSL modules). Built for plugging LLM agents (Claude,
Cursor, any MCP client) into the "internals" of a configuration: catalogs,
documents, registers, attributes, module procedures, and the call graph.

Transport: **streamable HTTP** (FastMCP 4.x), endpoint `http://<host>:8765/mcp`.

## Features

- Parses the configuration XML export (objects, elements, data types, cross-object
  references) — `src/config_parser.py`.
- Parses BSL modules: procedures/functions, visibility (`Экспорт`), calls,
  references to configuration objects (`Справочники.X`, `Документы.Y`, ...) —
  `src/bs_parser.py`.
- SQLite graph (objects → elements, modules → symbols, calls, references) —
  `src/graph.py`.
- Semantic search: external embedder (OpenAI-compatible `/embeddings`) →
  ANN search in Qdrant → external reranker (Cohere/Jina-compatible `/rerank`) —
  `src/embedder.py`, `src/reranker.py`.
- Incremental index: content hashes in SQLite, only changed nodes are re-embedded;
  removed objects are cleaned from both stores — `src/indexer.py`.
- Multi-source indexing: a `.txt` configuration report (ФАЗА 0) supplemented by
  the XML dump manifest (`ConfigDumpInfo.xml`) and per-object XML, plus a legacy
  `is_legacy` flag driven by run mode / `FormType` — `src/xml_manifest.py`,
  `src/xml_config.py`, `src/xml_object.py`.

## Requirements

- Python **3.11+**
- Qdrant (HTTP server or local on-disk mode)
- Embedder with an OpenAI-compatible API: Ollama (`/v1`), vLLM, TEI, Jina,
  OpenAI, Azure — anything answering `POST /embeddings` in OpenAI format.
- Reranker (optional): Cohere/Jina-compatible `POST /rerank`. Without a reranker
  `search_config` still works, returning ANN order (graceful fallback).

### Degradation without external services

| Component | Unavailable | Still works |
|---|---|---|
| No embedder | `search_config` (semantic search) | All structural tools: `list_objects`, `get_object_elements`, `get_symbol`, `get_callers/callees`, `get_references`, `graph_stats` (SQLite) |
| No reranker | Candidate reordering | `search_config` in ANN order |
| No Qdrant | `search_config`, vector upsert at index time | SQLite graph fully functional; index with `--no-vectors` |

Indexing without an embedder: `python -m src.indexer --no-vectors` builds only
the SQLite graph. Semantic search appears after setting `embedder.base_url` and
re-running indexing.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Priority (highest wins):

1. Environment variables (`ONEC_*`) — for secrets and deployment.
2. JSON config file (path from `ONEC_MCP_CONFIG`, default `./config.json`).
3. Defaults in `src/config.py`.

Template: `config.json.example`. Copy to `config.json` and fill in.

```json
{
  "config_root": "/path/to/onec/sources",
  "project_data_dir": "/path/to/onec/project-export",
  "xml_root": "/path/to/onec/code",
  "txt_root": "/path/to/onec/metadata",
  "qdrant": { "url": "http://localhost:6333", "collection": "onec_config" },
  "graph_db_path": "./data/graph.sqlite3",
  "embedder": {
    "base_url": "http://localhost:11434/v1",
    "api_key": "",
    "model": "bge-m3",
    "dimensions": 1024,
    "batch_size": 32
  },
  "reranker": {
    "endpoint": "",
    "api_key": "",
    "model": ""
  },
  "search": { "top_k": 5, "candidate_multiplier": 4 },
  "host": "0.0.0.0",
  "port": 8765,
  "auth_token": "",
  "payload_only": false,
  "node_id_in_payload": true
}
```

If `auth_token` (or `ONEC_AUTH_TOKEN`) is set, the server requires the
`Authorization: Bearer <token>` header on all HTTP requests. An empty value
disables authentication.

### Environment variables

| Variable | Description |
|---|---|
| `ONEC_CONFIG_ROOT` | Root of the 1C configuration source (XML/BSL) |
| `ONEC_PROJECT_DATA_DIR` | Project export directory (`metadata/`, `code/`) |
| `ONEC_QDRANT_URL` | Qdrant URL, e.g. http://localhost:6333 |
| `ONEC_QDRANT_API_KEY` | Qdrant API key (if enabled) |
| `ONEC_QDRANT_COLLECTION` | Collection name (default `onec_config`) |
| `ONEC_GRAPH_DB_PATH` | Path to the SQLite graph |
| `ONEC_EMBEDDER_BASE_URL` | Embedder base URL (OpenAI-compatible `/embeddings`) |
| `ONEC_EMBEDDER_API_KEY` | Embedder API key |
| `ONEC_EMBEDDER_MODEL` | Embedding model (default `bge-m3`) |
| `ONEC_EMBEDDER_DIMENSIONS` | Vector dimensionality (must match the model) |
| `ONEC_EMBEDDER_BATCH_SIZE` | Embedding batch size |
| `ONEC_RERANKER_ENDPOINT` | Reranker URL (Cohere/Jina-compatible), empty = off |
| `ONEC_RERANKER_API_KEY` | Reranker API key |
| `ONEC_RERANKER_MODEL` | Reranker model (optional) |
| `ONEC_SEARCH_TOP_K` | Number of results to return |
| `ONEC_HOST` | HTTP server address (default `0.0.0.0`) |
| `ONEC_PORT` | HTTP server port (default `8765`) |
| `ONEC_AUTH_TOKEN` | Auth token: if set, requires `Authorization: Bearer <token>` |
| `ONEC_XML_ROOT` | XML dump root (`ConfigDumpInfo.xml` + per-object XML); default `project_data_dir/code` |
| `ONEC_TXT_ROOT` | TXT report root (`ОтчетПоКонфигурации.txt`); default `project_data_dir/metadata` |
| `ONEC_PAYLOAD_ONLY` | `true` — update Qdrant payloads only, without re-embedding |
| `ONEC_NODE_ID_IN_PAYLOAD` | `true` (default) — store `node_id` in Qdrant payload for `search_config` |

For the exact list see `_ENV_MAP` in `src/config.py`.

## Source layout

The expected dump is a standard "configuration in source code" export.

The parser is tolerant of variations and accepts both flat elements and
property-value pairs. Object names are normalized to a full form.

A minimal sample for a manual run lives in `tests/sample_config/`.

## Indexing

```bash
# incremental (default): only changed nodes are re-embedded
python -m src.indexer

# full rebuild
python -m src.indexer --full

# no vectors (SQLite graph only) — for parser debugging
python -m src.indexer --no-vectors

# reindex a single object (Russian or English name)
python -m src.indexer --object Справочник.Колледжи
python -m src.indexer --object Catalog.Колледжи

# embed only one layer (object|element|symbol); the graph stays in SQLite
python -m src.indexer --kind symbol

# update Qdrant payloads only, without re-embedding (after flag changes)
python -m src.indexer --payload-only

# different config file
python -m src.indexer --config /path/to/config.json
```

The output is statistics: object count, BSL file count, graph nodes/edges,
updated vectors. Indexing is idempotent; when objects are removed from the
configuration their nodes and vectors are deleted from the stores.

## Running the MCP server

```bash
python -m src.server
# INFO: Starting MCP server '1c-configuration' with transport 'streamable-http'
#       on http://0.0.0.0:8765/mcp
```

The server reads the same configuration and keeps the graph, the embedder client
and the reranker client in memory. Vector queries to Qdrant run on the fly.

### Tools

| Tool | Purpose |
|---|---|
| `get_metadata(mode, category?, object_name?, object_match?, limit?, offset?)` | Inventory: `summary` (counts), `categories` (types), `objects` (filtered list) |
| `inspect_metadata_object(object_ref, detail?, sections?)` | Object dossier in one call: counts, structure, forms, BSL modules, usages |
| `get_metadata_object_structure(object_ref, sections?, tabular_part?)` | Object structure by section (attributes/tabular_parts/forms/commands/layouts/resources/dimensions) |
| `get_metadata_element_type(object_ref, element_type, container_ref?)` | Typed children of an object (attributes/resources/dimensions/...) |
| `get_metadata_details(ref_type, ref, owner_ref?, mode?)` | Resolve a reference into a node card (object/element/symbol) |
| `list_objects(type?)` | Configuration objects grouped by type, with element counts |
| `get_object_elements(object_name, include_children=true)` | All elements of an object: attributes, tabular sections, commands |

| Tool | Purpose |
|---|---|
| `search_config(query, top_k=5, kind?)` | Semantic search across objects/elements/procedures (embed → Qdrant → rerank) |
| `find_metadata_objects(search_by, search_text?, within_object?, limit?)` | Find objects by description or by a child element name ("where is field X") |
| `find_metadata_elements(element_type, element_name?, owner_object?, mode?, limit?)` | Child elements across the project with owner context |
| `find_metadata_usages(target_ref, mode?)` | Who references an object / which modules use it |

| Tool | Purpose |
|---|---|
| `search_bsl_code(query, top_k=5)` | Semantic search over procedure/function bodies |
| `get_symbol(name, object_name?)` | Procedure/function: signature, visibility, module, body |
| `get_callers(name, object_name?)` | Who calls this procedure (incoming edges) |
| `get_callees(name, object_name?)` | What this procedure calls (outgoing edges) |
| `get_bsl_call_graph(routine_ref, mode?, depth?, owner_ref?)` | Call graph: `callees`/`callers`/`subtree` (BFS with depth) |
| `get_bsl_routine_body(routine_ref, owner_ref?, body_offset?, body_limit?)` | Routine body with pagination |
| `get_bsl_modules(mode, owner_ref?, module_ref?, routine_name?)` | Modules of an object and their routines |
| `search_bsl_routines(name?, mode?, object_name?, exported_only?, limit?)` | Search routines by name/export/signature |

| Tool | Purpose |
|---|---|
| `get_references(object_name, direction="both")` | References to/from an object |
| `reindex(full=false)` | Rebuild the index in a background thread |
| `reindex_status` | Status of the background reindex |
| `graph_stats` | Graph statistics: nodes/edges by kind |

Remaining roadmap (full map in `PLAN.md` §13):

- **Tier B** (needs submodule parsers wired into the graph): `find_predefined_values`,
  `get_event_subscriptions`, `get_access_rights`.
- **Tier C** (deferred, no data model): `get_extension_object_diff`,
  `get_form_structure`/`find_form_links`, `find_dependency_paths`.

### Connecting an MCP client

Endpoint: `http://<host>:<port>/mcp` (streamable HTTP transport).

Claude Desktop / Claude Code:

```json
{
  "mcpServers": {
    "1c": {
      "url": "http://localhost:8765/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

The `headers` field is only needed if `auth_token` is set.

Python (FastMCP client):

```python
from fastmcp import Client

async def main():
    async with Client("http://localhost:8765/mcp") as c:
        print(await c.call_tool("list_objects", {}))
        print(await c.call_tool("search_config", {"query": "цена товара"}))
```

## Docker

`docker-compose.yml` brings up Qdrant + the server. The configuration sources
are mounted at `/data/config`; the embedder is expected at
`ONEC_EMBEDDER_BASE_URL` (e.g. Ollama on the host:
`http://host.docker.internal:11434/v1`).

```bash
# 1. fill in .env (see .env.example) and config.json
# 2. load the embedding model into Ollama (or another service):
#    ollama pull bge-m3
docker compose up -d --build

# indexing runs automatically at startup (entrypoint.sh)
docker compose logs -f mcp
```

Manually: `docker compose run --rm mcp python -m src.indexer --full`.

## Architecture

```
1C XML/BSL export
        │
        ▼
src/config_parser.py ── objects, elements, references (data types)
src/report_parser.py ── ОтчетПоКонфигурации.txt (ФАЗА 0 — primary source)
src/xml_manifest.py ─── ConfigDumpInfo.xml (tree, source_key, configVersion)
src/xml_config.py ───── Configuration.xml (run/interface/compatibility mode)
src/bs_parser.py ────── procedures, calls, code references
        │
        ▼
src/indexer.py ───────── incremental build
   ├──► src/graph.py    SQLite: nodes (object/element/symbol/module),
   │                    edges (HAS_ELEMENT, CHILD_ELEMENT, REFERENCE,
   │                    DEFINES, CALLS, USES)
   └──► Qdrant          vectors (embedder: OpenAI-compatible API)
        │
        ▼
src/server.py (FastMCP, streamable HTTP :8765/mcp)
   list_objects / get_object_elements / search_config (embed→Qdrant→rerank)
   get_symbol / get_callers / get_callees / get_references / reindex / graph_stats
```

Edge kinds:

- `HAS_ELEMENT` — object → element (direct children)
- `CHILD_ELEMENT` — element → nested element
- `REFERENCE` — element → object (data types, e.g. attribute → catalog)
- `DEFINES` — module → symbol
- `CALLS` — symbol → symbol (1C has a flat global namespace, so a call may
  resolve to several same-named symbols — this is normal)
- `USES` — module → object (`Справочники.X` etc. in code)

## Tests

```bash
# unit tests (parsers/mapping/legacy flag/checksums)
pytest tests/

# compile all modules
python -m compileall src
```

## Limitations

- The BSL parser is line/regex-based, not a full grammar: adequate for the call
  graph and search, but not for strict code validation.
- **Preprocessor directives are not expanded.** The BSL parser does not handle
  `#Если`/`#Иначе`/`#КонецЕсли`, so a procedure declared in both branches (e.g. a
  server-side `Печать` for ordinary unmanaged forms inside
  `#Если ТолстыйКлиентОбычноеПриложение ... #Иначе ... #КонецЕсли`) yields two
  declarations with the same `stable_id`. This does not affect search/graph
  (upsert by id overwrites), but a symbol count may show a duplicate.
- Calls resolve by name (1C's flat global namespace): with same-named procedures
  in different modules, `CALLS` edges lead to all candidates; `get_symbol` takes
  `object_name` for disambiguation.
- The vector dimensionality (`embedder.dimensions`) must match the model,
  otherwise Qdrant rejects the upsert.
- **Objects outside the `.txt` report** (business processes, common
  modules/forms, web services, etc.) are indexed as module/symbol, but without an
  object node and `HAS_MODULE` edge (see `PLAN.md` §12).
- **Legacy forms** (`FormType=Ordinary`, binary `Form.bin`) are not indexed —
  they await an external binary parser (`parse_legacy_object`, phase-2).
- **Qdrant RAM** is the only real volume limit: ~6 KB/vector (1536 dims float32),
  tens of GB of RAM for ERP 2.x-scale configurations (see `PLAN.md` §12).