"""Embedded property-graph engine backed by SQLite.

This is a real graph store, not a dict: nodes and edges live in indexed
relations, property predicates compile to ``json_extract`` expressions against
expression indexes, and multi-hop traversal is a capped BFS over the adjacency
index -- the local analogue of HydraDB's ``algo.SSpaths``.

It exists so Weave runs end to end with zero external services. The HydraDB
backend in ``hydra.py`` implements the same contract over Bolt/OpenCypher.

Nodes carry exactly one label in Weave; ``primary_label`` is stored as a real
column so label scans and the composite expression indexes are index-assisted.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path as FsPath
from typing import Any, Iterator, Sequence

from ..util import new_id
from . import schema
from .store import (
    Direction,
    Edge,
    Filter,
    GraphStore,
    Node,
    OrderBy,
    Path,
    Props,
    Tx,
    normalise_filter,
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id            TEXT PRIMARY KEY,
    primary_label TEXT NOT NULL,
    labels        TEXT NOT NULL,
    props         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    id       TEXT PRIMARY KEY,
    type     TEXT NOT NULL,
    start_id TEXT NOT NULL,
    end_id   TEXT NOT NULL,
    props    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_label     ON nodes(primary_label);
CREATE INDEX IF NOT EXISTS idx_edges_out       ON edges(start_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_in        ON edges(end_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_type      ON edges(type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_uniq ON edges(start_id, end_id, type);
"""


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def _bind(value: Any) -> Any:
    """Map a Python value onto what ``json_extract`` returns for it."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return value


def _compile_where(where: Filter | None, alias: str = "n") -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for key, cmp in normalise_filter(where):
        col = f"json_extract({alias}.props, '$.{_ident(key)}')"
        if cmp.op == "IS NULL":
            clauses.append(f"{col} IS NULL")
        elif cmp.op == "IS NOT NULL":
            clauses.append(f"{col} IS NOT NULL")
        elif cmp.op == "IN":
            values = [_bind(v) for v in (cmp.value or [])]
            if not values:
                clauses.append("0 = 1")
                continue
            clauses.append(f"{col} IN ({','.join('?' * len(values))})")
            params.extend(values)
        elif cmp.op == "CONTAINS":
            clauses.append(f"{col} LIKE ?")
            params.append(f"%{cmp.value}%")
        elif cmp.op in ("=", "<>", "<", "<=", ">", ">="):
            bound = _bind(cmp.value)
            if bound is None:
                clauses.append(f"{col} IS {'NOT ' if cmp.op == '<>' else ''}NULL")
            else:
                clauses.append(f"{col} {cmp.op} ?")
                params.append(bound)
        else:
            raise ValueError(f"unsupported operator: {cmp.op}")
    return (" AND ".join(clauses) if clauses else "1 = 1", params)


def _compile_order(order_by: OrderBy | None, alias: str = "n") -> str:
    if not order_by:
        return ""
    parts = []
    for prop, direction in order_by:
        arrow = "DESC" if str(direction).lower().startswith("d") else "ASC"
        parts.append(f"json_extract({alias}.props, '$.{_ident(prop)}') {arrow}")
    return " ORDER BY " + ", ".join(parts)


def _row_to_node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"],
        labels=tuple(json.loads(row["labels"])),
        props=json.loads(row["props"]),
    )


def _row_to_edge(row: sqlite3.Row) -> Edge:
    return Edge(
        id=row["id"],
        type=row["type"],
        start_id=row["start_id"],
        end_id=row["end_id"],
        props=json.loads(row["props"]),
    )


class SqliteTx(Tx):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- writes ------------------------------------------------------------

    def create_node(self, labels: Sequence[str], props: Props) -> Node:
        labels = tuple(labels)
        if not labels:
            raise ValueError("a node needs at least one label")
        props = dict(props)
        props.setdefault("id", new_id())
        self._conn.execute(
            "INSERT INTO nodes (id, primary_label, labels, props) VALUES (?, ?, ?, ?)",
            (
                props["id"],
                labels[0],
                json.dumps(list(labels)),
                json.dumps(props, default=str),
            ),
        )
        return Node(id=props["id"], labels=labels, props=props)

    def merge_node(
        self,
        label: str,
        key: Props,
        on_create: Props | None = None,
        on_match: Props | None = None,
    ) -> tuple[Node, bool]:
        existing = self.match(label, where=key, limit=1)
        if existing:
            node = existing[0]
            if on_match:
                node = self.set_props(node.id, on_match)
            return node, False
        props = {**dict(key), **dict(on_create or {})}
        return self.create_node([label], props), True

    def set_props(self, node_id: str, props: Props) -> Node:
        row = self._conn.execute(
            "SELECT id, primary_label, labels, props FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"node not found: {node_id}")
        merged = json.loads(row["props"])
        merged.update(props)
        self._conn.execute(
            "UPDATE nodes SET props = ? WHERE id = ?",
            (json.dumps(merged, default=str), node_id),
        )
        return Node(id=node_id, labels=tuple(json.loads(row["labels"])), props=merged)

    def create_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> Edge:
        edge_props = dict(props or {})
        edge_id = edge_props.get("id") or new_id("e")
        edge_props["id"] = edge_id
        self._conn.execute(
            "INSERT OR REPLACE INTO edges (id, type, start_id, end_id, props) "
            "VALUES (?, ?, ?, ?, ?)",
            (edge_id, rel_type, start_id, end_id, json.dumps(edge_props, default=str)),
        )
        return Edge(
            id=edge_id, type=rel_type, start_id=start_id, end_id=end_id, props=edge_props
        )

    def upsert_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> Edge:
        row = self._conn.execute(
            "SELECT id FROM edges WHERE start_id = ? AND end_id = ? AND type = ?",
            (start_id, end_id, rel_type),
        ).fetchone()
        edge_props = dict(props or {})
        if row is not None:
            edge_props["id"] = row["id"]
        return self.create_edge(start_id, end_id, rel_type, edge_props)

    def merge_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> tuple[Edge, bool]:
        row = self._conn.execute(
            "SELECT id, type, start_id, end_id, props FROM edges "
            "WHERE start_id = ? AND end_id = ? AND type = ?",
            (start_id, end_id, rel_type),
        ).fetchone()
        if row is not None:
            return _row_to_edge(row), False
        return self.create_edge(start_id, end_id, rel_type, props), True

    # -- reads -------------------------------------------------------------

    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute(
            "SELECT id, primary_label, labels, props FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _row_to_node(row) if row else None

    def match(
        self,
        label: str | None = None,
        where: Filter | None = None,
        order_by: OrderBy | None = None,
        limit: int | None = None,
        skip: int | None = None,
    ) -> list[Node]:
        clause, params = _compile_where(where)
        sql = "SELECT id, primary_label, labels, props FROM nodes n WHERE " + clause
        if label:
            sql += " AND n.primary_label = ?"
            params.append(label)
        sql += _compile_order(order_by)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
            if skip:
                sql += " OFFSET ?"
                params.append(int(skip))
        return [_row_to_node(r) for r in self._conn.execute(sql, params).fetchall()]

    def count(self, label: str | None = None, where: Filter | None = None) -> int:
        clause, params = _compile_where(where)
        sql = "SELECT COUNT(*) AS c FROM nodes n WHERE " + clause
        if label:
            sql += " AND n.primary_label = ?"
            params.append(label)
        return int(self._conn.execute(sql, params).fetchone()["c"])

    def count_edges(self, rel_type: str | None = None) -> int:
        if rel_type:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM edges WHERE type = ?", (rel_type,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()
        return int(row["c"])

    def expand(
        self,
        node_ids: Sequence[str],
        rel_types: Sequence[str] | None = None,
        direction: Direction = "both",
        target_label: str | None = None,
        target_where: Filter | None = None,
        limit: int | None = None,
    ) -> list[tuple[Node, Edge, Node]]:
        node_ids = list(dict.fromkeys(node_ids))
        if not node_ids:
            return []
        out: list[tuple[Node, Edge, Node]] = []
        for edge, anchor_id, other_id in self._adjacent(node_ids, rel_types, direction):
            anchor = self.get_node(anchor_id)
            other = self.get_node(other_id)
            if anchor is None or other is None:
                continue
            if target_label and other.labels[0] != target_label:
                continue
            if target_where and not _matches(other, target_where):
                continue
            out.append((anchor, edge, other))
            if limit is not None and len(out) >= limit:
                break
        return out

    def _adjacent(
        self,
        node_ids: Sequence[str],
        rel_types: Sequence[str] | None,
        direction: Direction,
    ) -> list[tuple[Edge, str, str]]:
        placeholders = ",".join("?" * len(node_ids))
        type_clause = ""
        type_params: list[Any] = []
        if rel_types:
            type_clause = f" AND type IN ({','.join('?' * len(rel_types))})"
            type_params = list(rel_types)

        results: list[tuple[Edge, str, str]] = []
        if direction in ("out", "both"):
            rows = self._conn.execute(
                "SELECT id, type, start_id, end_id, props FROM edges "
                f"WHERE start_id IN ({placeholders}){type_clause}",
                list(node_ids) + type_params,
            ).fetchall()
            results.extend(
                (_row_to_edge(r), r["start_id"], r["end_id"]) for r in rows
            )
        if direction in ("in", "both"):
            rows = self._conn.execute(
                "SELECT id, type, start_id, end_id, props FROM edges "
                f"WHERE end_id IN ({placeholders}){type_clause}",
                list(node_ids) + type_params,
            ).fetchall()
            results.extend((_row_to_edge(r), r["end_id"], r["start_id"]) for r in rows)
        return results

    def paths(
        self,
        source_ids: Sequence[str],
        rel_types: Sequence[str] | None = None,
        direction: Direction = "both",
        max_len: int = 3,
        path_count: int = 10,
        result_limit: int = 50,
        target_labels: Sequence[str] | None = None,
    ) -> list[Path]:
        """Capped BFS -- the embedded analogue of ``algo.SSpaths``."""
        collected: list[Path] = []
        wanted = set(target_labels) if target_labels else None

        for source_id in dict.fromkeys(source_ids):
            source = self.get_node(source_id)
            if source is None:
                continue
            per_source = 0
            frontier: list[Path] = [Path(nodes=(source,), edges=())]
            for _ in range(max(0, max_len)):
                if per_source >= path_count or len(collected) >= result_limit:
                    break
                next_frontier: list[Path] = []
                ends = [p.end.id for p in frontier]
                if not ends:
                    break
                adjacency: dict[str, list[tuple[Edge, str]]] = {}
                for edge, anchor_id, other_id in self._adjacent(
                    ends, rel_types, direction
                ):
                    adjacency.setdefault(anchor_id, []).append((edge, other_id))

                for partial in frontier:
                    for edge, other_id in adjacency.get(partial.end.id, []):
                        if any(n.id == other_id for n in partial.nodes):
                            continue  # no cycles within a single path
                        other = self.get_node(other_id)
                        if other is None:
                            continue
                        extended = Path(
                            nodes=partial.nodes + (other,),
                            edges=partial.edges + (edge,),
                        )
                        next_frontier.append(extended)
                        if wanted is None or other.labels[0] in wanted:
                            collected.append(extended)
                            per_source += 1
                            if (
                                per_source >= path_count
                                or len(collected) >= result_limit
                            ):
                                break
                    if per_source >= path_count or len(collected) >= result_limit:
                        break
                frontier = next_frontier
        return collected[:result_limit]


def _matches(node: Node, where: Filter) -> bool:
    for key, cmp in normalise_filter(where):
        value = node.props.get(key)
        if cmp.op == "=" and value != cmp.value:
            return False
        if cmp.op == "<>" and value == cmp.value:
            return False
        if cmp.op == "IN" and value not in (cmp.value or []):
            return False
        if cmp.op == "IS NULL" and value is not None:
            return False
        if cmp.op == "IS NOT NULL" and value is None:
            return False
        if cmp.op == "CONTAINS" and cmp.value not in str(value or ""):
            return False
        if cmp.op in ("<", "<=", ">", ">="):
            if value is None:
                return False
            ops = {
                "<": value < cmp.value,
                "<=": value <= cmp.value,
                ">": value > cmp.value,
                ">=": value >= cmp.value,
            }
            if not ops[cmp.op]:
                return False
    return True


class EmbeddedGraphStore(GraphStore):
    """SQLite-backed property graph. Default backend; no services required."""

    name = "embedded"

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            FsPath(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        self.ensure_schema()

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        with self._lock:
            tx = SqliteTx(self._conn)
            if self._depth > 0:  # re-entrant: join the outer transaction
                self._depth += 1
                try:
                    yield tx
                finally:
                    self._depth -= 1
                return
            self._depth = 1
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield tx
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                self._depth = 0

    def ensure_schema(self) -> list[str]:
        """Create the expression indexes named in the specification."""
        applied: list[str] = []
        with self._lock:
            for name, label, prop in schema.INDEX_DEFINITIONS:
                stmt = (
                    f"CREATE INDEX IF NOT EXISTS {_ident(name)} ON nodes "
                    f"(primary_label, json_extract(props, '$.{_ident(prop)}'))"
                )
                self._conn.execute(stmt)
                applied.append(f"{name} :{label}({prop})")
            self._conn.commit()
        return applied

    def verify(self) -> bool:
        with self.transaction() as tx:
            probe = tx.create_node(["__probe"], {"n": 1})
            found = tx.get_node(probe.id)
            self._conn.execute("DELETE FROM nodes WHERE id = ?", (probe.id,))
            return found is not None and found.props.get("n") == 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            labels = {
                row["primary_label"]: row["c"]
                for row in self._conn.execute(
                    "SELECT primary_label, COUNT(*) AS c FROM nodes "
                    "WHERE primary_label NOT LIKE '\\_\\_%' ESCAPE '\\' "
                    "GROUP BY primary_label"
                )
            }
            rels = {
                row["type"]: row["c"]
                for row in self._conn.execute(
                    "SELECT type, COUNT(*) AS c FROM edges GROUP BY type"
                )
            }
        layers = {"episodic": 0, "semantic": 0, "procedural": 0}
        for label, count in labels.items():
            layer = schema.LAYER_OF_LABEL.get(label)
            if layer:
                layers[layer] += count
        return {
            "backend": self.name,
            "location": self.path,
            "nodes": sum(labels.values()),
            "edges": sum(rels.values()),
            "by_label": labels,
            "by_relationship": rels,
            "by_layer": layers,
        }

    def reset(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM edges")
            self._conn.execute("DELETE FROM nodes")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
