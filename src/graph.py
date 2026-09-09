"""SQLite-backed graph store for the 1C configuration.

Nodes:
  object   - a configuration object (Каталог.Номенклатура)
  element  - an object element (attribute, tabular section, command...)
  symbol   - a procedure/function from BSL code
  module   - a .bsl file

Edges:
  HAS_ELEMENT      object  -> element (direct children)
  CHILD_ELEMENT    element -> element (nested)
  REFERENCE        element -> object  (data type reference, e.g. attribute -> catalog)
  DEFINES          module  -> symbol
  CALLS            symbol  -> symbol  (by name; 1C global namespace => may be multi-target)
  USES             module  -> object  (Справочники.X etc. in code)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    object_name TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL DEFAULT '',
    props       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_object ON nodes(object_name);

CREATE TABLE IF NOT EXISTS edges (
    src  TEXT NOT NULL,
    dst  TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, kind);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, kind);

CREATE TABLE IF NOT EXISTS node_hashes (
    node_id TEXT PRIMARY KEY,
    hash    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS object_checksums (
    source_key TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class GraphStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------- writes ---

    def upsert_nodes(self, nodes: Iterable[dict[str, Any]]) -> None:
        rows = [
            (n["id"], n["kind"], n.get("name", ""), n.get("object_name", ""),
             n.get("type", ""), json.dumps(n.get("props", {}), ensure_ascii=False))
            for n in nodes
        ]
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO nodes (id, kind, name, object_name, type, props) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()

    def upsert_edges(self, edges: Iterable[tuple[str, str, str]]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO edges (src, dst, kind) VALUES (?, ?, ?)", edges
            )
            self.conn.commit()

    def set_node_hashes(self, hashes: dict[str, str]) -> None:
        with self._lock:
            self.conn.executemany("DELETE FROM node_hashes WHERE node_id = ?",
                                  [(k,) for k in hashes])
            self.conn.executemany(
                "INSERT INTO node_hashes (node_id, hash) VALUES (?, ?)",
                list(hashes.items()),
            )
            self.conn.commit()

    def get_node_hashes(self) -> dict[str, str]:
        with self._lock:
            cur = self.conn.execute("SELECT node_id, hash FROM node_hashes")
            return {r[0]: r[1] for r in cur.fetchall()}

    def set_object_checksums(self, checksums: dict[str, str]) -> None:
        with self._lock:
            now = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
            self.conn.executemany(
                "INSERT OR REPLACE INTO object_checksums (source_key, checksum, updated_at) "
                "VALUES (?, ?, ?)",
                [(k, v, now) for k, v in checksums.items()],
            )
            self.conn.commit()

    def get_object_checksums(self) -> dict[str, str]:
        with self._lock:
            cur = self.conn.execute("SELECT source_key, checksum FROM object_checksums")
            return {r[0]: r[1] for r in cur.fetchall()}

    def clear_object_checksums(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM object_checksums")
            self.conn.commit()

    def delete_stale_nodes(self, keep_ids: set[str]) -> list[str]:
        """Delete nodes (and their edges/hashes) not in keep_ids. Returns dropped IDs."""
        with self._lock:
            if not keep_ids:
                cur = self.conn.execute("SELECT id FROM nodes")
                drop = [r[0] for r in cur.fetchall()]
                if drop:
                    self.conn.execute("DELETE FROM nodes")
                    self.conn.execute("DELETE FROM edges")
                    self.conn.execute("DELETE FROM node_hashes")
                self.conn.commit()
                return drop
            cur = self.conn.execute("SELECT id FROM nodes")
            drop = [r[0] for r in cur.fetchall() if r[0] not in keep_ids]
            if drop:
                self._delete_by_ids(drop)
            self.conn.commit()
            return drop

    def _delete_by_ids(self, ids: list[str]) -> None:
        """Chunked delete of nodes/edges/hashes to stay under SQLite param limit."""
        for i in range(0, len(ids), self._SQLITE_PARAM_LIMIT):
            chunk = ids[i : i + self._SQLITE_PARAM_LIMIT]
            ph = ",".join("?" * len(chunk))
            self.conn.execute(f"DELETE FROM edges WHERE src IN ({ph}) OR dst IN ({ph})", chunk + chunk)
            self.conn.execute(f"DELETE FROM nodes WHERE id IN ({ph})", chunk)
            self.conn.execute(f"DELETE FROM node_hashes WHERE node_id IN ({ph})", chunk)

    def clear(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM nodes")
            self.conn.execute("DELETE FROM edges")
            self.conn.execute("DELETE FROM node_hashes")
            self.conn.commit()

    # ------------------------------------------------------------- queries ---

    @staticmethod
    def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            d = dict(r)
            if "props" in d and isinstance(d.get("props"), str):
                try:
                    d["props"] = json.loads(d["props"])
                except json.JSONDecodeError:
                    pass
            out.append(d)
        return out

    def objects(self, type_filter: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if type_filter:
                cur = self.conn.execute(
                    "SELECT * FROM nodes WHERE kind='object' AND type=? ORDER BY name",
                    (type_filter,),
                )
            else:
                cur = self.conn.execute("SELECT * FROM nodes WHERE kind='object' ORDER BY name")
            return self._rows_to_dicts(cur.fetchall())

    def object_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM nodes WHERE kind='object' AND (name=? OR object_name=?)",
                (name, name),
            )
            row = cur.fetchone()
            return self._rows_to_dicts([row])[0] if row else None

    def element_by(self, object_name: str, name: str) -> dict[str, Any] | None:
        """Find a direct element (e.g. a tabular section) of an object by name."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT n.* FROM nodes n "
                "JOIN edges e ON e.src = o.id AND e.dst = n.id AND e.kind='HAS_ELEMENT' "
                "JOIN nodes o ON o.id = e.src AND o.kind='object' "
                "WHERE o.name=? AND n.name=? AND n.kind='element' LIMIT 1",
                (object_name, name),
            )
            row = cur.fetchone()
            return self._rows_to_dicts([row])[0] if row else None

    def elements_of(self, object_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT n.* FROM nodes n JOIN edges e ON e.dst = n.id "
                "WHERE e.src = ? AND e.kind='HAS_ELEMENT' ORDER BY n.name",
                (object_id,),
            )
            return self._rows_to_dicts(cur.fetchall())

    def symbols_by_name(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM nodes WHERE kind='symbol' AND name=? ORDER BY object_name",
                (name,),
            )
            return self._rows_to_dicts(cur.fetchall())

    _SQLITE_PARAM_LIMIT = 900

    def _neighbors(
        self, ids: list[str], edge_kind: str, direction: str
    ) -> list[dict[str, Any]]:
        """Neighbors via an edge kind. direction='in': dst IN ids; 'out': src IN ids."""
        if not ids:
            return []
        seen: dict[str, dict[str, Any]] = {}
        col = "dst" if direction == "in" else "src"
        join_col = "src" if direction == "in" else "dst"
        with self._lock:
            for i in range(0, len(ids), self._SQLITE_PARAM_LIMIT):
                chunk = ids[i : i + self._SQLITE_PARAM_LIMIT]
                ph = ",".join("?" * len(chunk))
                cur = self.conn.execute(
                    f"SELECT DISTINCT n.* FROM nodes n "
                    f"JOIN edges e ON e.{join_col} = n.id "
                    f"WHERE e.kind=? AND e.{col} IN ({ph}) ORDER BY n.name",
                    [edge_kind, *chunk],
                )
                for d in self._rows_to_dicts(cur.fetchall()):
                    seen[d["id"]] = d
        return list(seen.values())

    def callers(self, symbol_ids: list[str]) -> list[dict[str, Any]]:
        return self._neighbors(symbol_ids, "CALLS", direction="in")

    def callees(self, symbol_ids: list[str]) -> list[dict[str, Any]]:
        return self._neighbors(symbol_ids, "CALLS", direction="out")

    def references_to(self, object_id: str) -> list[dict[str, Any]]:
        """Elements that reference this object (data type refs)."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT n.* FROM nodes n JOIN edges e ON e.src = n.id "
                "WHERE e.dst = ? AND e.kind='REFERENCE' ORDER BY n.name",
                (object_id,),
            )
            return self._rows_to_dicts(cur.fetchall())

    def references_from(self, object_id: str) -> list[dict[str, Any]]:
        """Objects this object's elements reference."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT DISTINCT n.* FROM nodes n "
                "JOIN edges e1 ON e1.src = ? AND e1.kind='HAS_ELEMENT' "
                "JOIN edges e2 ON e2.src = e1.dst AND e2.kind='REFERENCE' "
                "WHERE e2.dst = n.id ORDER BY n.name",
                (object_id,),
            )
            return self._rows_to_dicts(cur.fetchall())

    def modules_using(self, object_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT n.* FROM nodes n JOIN edges e ON e.src = n.id "
                "WHERE e.dst = ? AND e.kind='USES' ORDER BY n.name",
                (object_id,),
            )
            return self._rows_to_dicts(cur.fetchall())

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self._lock:
            cur = self.conn.execute("SELECT kind, COUNT(*) c FROM nodes GROUP BY kind")
            for r in cur.fetchall():
                out[r["kind"]] = r["c"]
            cur = self.conn.execute("SELECT kind, COUNT(*) c FROM edges GROUP BY kind")
            for r in cur.fetchall():
                out[f"edge:{r['kind']}"] = r["c"]
        return out

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            cur = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
