"""HydraDB backend: the same GraphStore contract over Bolt / OpenCypher.

Every operation the services need is expressed here as OpenCypher, and the
bounded-traversal primitive uses HydraDB's native ``algo.SSpaths`` /
``algo.MSpaths`` path procedures (specification section 8.2) rather than
client-side fan-out, falling back to a variable-length pattern match if the
procedures are unavailable.

Selected with ``WEAVE_BACKEND=hydra``. Requires the ``neo4j`` driver and a
reachable HydraDB endpoint.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
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

log = logging.getLogger("weave.hydra")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DIRECTION_TO_ALGO = {"out": "outgoing", "in": "incoming", "both": "both"}


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def _compile_where(
    where: Filter | None, alias: str = "n", prefix: str = "w"
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (key, cmp) in enumerate(normalise_filter(where)):
        prop = f"{alias}.{_ident(key)}"
        param = f"{prefix}{index}"
        if cmp.op == "IS NULL":
            clauses.append(f"{prop} IS NULL")
        elif cmp.op == "IS NOT NULL":
            clauses.append(f"{prop} IS NOT NULL")
        elif cmp.op == "IN":
            clauses.append(f"{prop} IN ${param}")
            params[param] = list(cmp.value or [])
        elif cmp.op == "CONTAINS":
            # Lower-cased on both sides so CONTAINS means the same thing here
            # as it does on the embedded backend, whose SQL LIKE is already
            # case-insensitive for ASCII. Without this the same filter returns
            # different rows depending on which backend is configured.
            clauses.append(f"toLower({prop}) CONTAINS toLower(${param})")
            params[param] = cmp.value
        elif cmp.op in ("=", "<>", "<", "<=", ">", ">="):
            if cmp.value is None:
                clauses.append(f"{prop} IS {'NOT ' if cmp.op == '<>' else ''}NULL")
            else:
                clauses.append(f"{prop} {cmp.op} ${param}")
                params[param] = cmp.value
        else:
            raise ValueError(f"unsupported operator: {cmp.op}")
    return (" AND ".join(clauses) if clauses else "true", params)


def _compile_order(order_by: OrderBy | None, alias: str = "n") -> str:
    if not order_by:
        return ""
    parts = [
        f"{alias}.{_ident(prop)} "
        + ("DESC" if str(direction).lower().startswith("d") else "ASC")
        for prop, direction in order_by
    ]
    return " ORDER BY " + ", ".join(parts)


def _to_node(raw: Any) -> Node:
    props = dict(raw)
    props.pop("__created", None)
    return Node(
        id=props.get("id") or str(getattr(raw, "element_id", "")),
        labels=tuple(getattr(raw, "labels", ()) or ("Node",)),
        props=props,
    )


def _to_edge(raw: Any) -> Edge:
    props = dict(raw)
    start = getattr(raw, "start_node", None)
    end = getattr(raw, "end_node", None)
    return Edge(
        id=props.get("id") or str(getattr(raw, "element_id", "")),
        type=getattr(raw, "type", "REL"),
        start_id=(dict(start).get("id") if start is not None else ""),
        end_id=(dict(end).get("id") if end is not None else ""),
        props=props,
    )


def _to_path(raw: Any) -> Path:
    return Path(
        nodes=tuple(_to_node(n) for n in raw.nodes),
        edges=tuple(_to_edge(r) for r in raw.relationships),
    )


class HydraTx(Tx):
    def __init__(self, tx: Any, supports_algo: bool = False) -> None:
        self._tx = tx
        self._supports_algo = supports_algo

    def _run(self, cypher: str, **params: Any) -> list[Any]:
        return list(self._tx.run(cypher, **params))

    # -- writes ------------------------------------------------------------

    def create_node(self, labels: Sequence[str], props: Props) -> Node:
        labels = tuple(labels)
        if not labels:
            raise ValueError("a node needs at least one label")
        props = dict(props)
        props.setdefault("id", new_id())
        label_clause = ":".join(_ident(label) for label in labels)
        rows = self._run(f"CREATE (n:{label_clause}) SET n += $props RETURN n", props=props)
        return _to_node(rows[0]["n"])

    def merge_node(
        self,
        label: str,
        key: Props,
        on_create: Props | None = None,
        on_match: Props | None = None,
    ) -> tuple[Node, bool]:
        key = dict(key)
        on_create = {**dict(on_create or {})}
        on_create.setdefault("id", new_id())
        key_clause = ", ".join(f"{_ident(k)}: $key.{_ident(k)}" for k in key)
        rows = self._run(
            f"MERGE (n:{_ident(label)} {{{key_clause}}}) "
            "ON CREATE SET n += $on_create, n.__created = true "
            "ON MATCH SET n += $on_match, n.__created = false "
            "WITH n, n.__created AS created "
            "REMOVE n.__created "
            "RETURN n, created",
            key=key,
            on_create=on_create,
            on_match=dict(on_match or {}),
        )
        return _to_node(rows[0]["n"]), bool(rows[0]["created"])

    def set_props(self, node_id: str, props: Props) -> Node:
        rows = self._run(
            "MATCH (n {id: $id}) SET n += $props RETURN n", id=node_id, props=dict(props)
        )
        if not rows:
            raise KeyError(f"node not found: {node_id}")
        return _to_node(rows[0]["n"])

    def create_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> Edge:
        edge_props = dict(props or {})
        edge_props.setdefault("id", new_id("e"))
        rows = self._run(
            "MATCH (a {id: $start}), (b {id: $end}) "
            f"CREATE (a)-[r:{_ident(rel_type)}]->(b) SET r += $props RETURN r, a, b",
            start=start_id,
            end=end_id,
            props=edge_props,
        )
        if not rows:
            raise KeyError(f"cannot connect {start_id} -> {end_id}")
        return Edge(
            id=edge_props["id"],
            type=rel_type,
            start_id=start_id,
            end_id=end_id,
            props=edge_props,
        )

    def upsert_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> Edge:
        edge_props = dict(props or {})
        edge_props.setdefault("id", new_id("e"))
        # MERGE, not CREATE: CREATE would add a parallel relationship on every
        # write, leaving several edges each holding a partial count.
        rows = self._run(
            "MATCH (a {id: $start}), (b {id: $end}) "
            f"MERGE (a)-[r:{_ident(rel_type)}]->(b) SET r += $props RETURN r",
            start=start_id,
            end=end_id,
            props=edge_props,
        )
        if not rows:
            raise KeyError(f"cannot connect {start_id} -> {end_id}")
        edge = _to_edge(rows[0]["r"])
        return Edge(
            id=edge.id, type=rel_type, start_id=start_id, end_id=end_id, props=edge.props
        )

    def merge_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> tuple[Edge, bool]:
        edge_props = dict(props or {})
        edge_props.setdefault("id", new_id("e"))
        rows = self._run(
            "MATCH (a {id: $start}), (b {id: $end}) "
            f"MERGE (a)-[r:{_ident(rel_type)}]->(b) "
            "ON CREATE SET r += $props, r.__created = true "
            "ON MATCH SET r.__created = false "
            "WITH r, r.__created AS created REMOVE r.__created RETURN r, created",
            start=start_id,
            end=end_id,
            props=edge_props,
        )
        if not rows:
            raise KeyError(f"cannot connect {start_id} -> {end_id}")
        edge = _to_edge(rows[0]["r"])
        return (
            Edge(
                id=edge.id,
                type=rel_type,
                start_id=start_id,
                end_id=end_id,
                props=edge.props,
            ),
            bool(rows[0]["created"]),
        )

    # -- reads -------------------------------------------------------------

    def get_node(self, node_id: str) -> Node | None:
        rows = self._run("MATCH (n {id: $id}) RETURN n LIMIT 1", id=node_id)
        return _to_node(rows[0]["n"]) if rows else None

    def match(
        self,
        label: str | None = None,
        where: Filter | None = None,
        order_by: OrderBy | None = None,
        limit: int | None = None,
        skip: int | None = None,
    ) -> list[Node]:
        clause, params = _compile_where(where)
        label_clause = f":{_ident(label)}" if label else ""
        cypher = f"MATCH (n{label_clause}) WHERE {clause} RETURN n"
        cypher += _compile_order(order_by)
        if skip:
            cypher += f" SKIP {int(skip)}"
        if limit is not None:
            cypher += f" LIMIT {int(limit)}"
        return [_to_node(row["n"]) for row in self._run(cypher, **params)]

    def count(self, label: str | None = None, where: Filter | None = None) -> int:
        clause, params = _compile_where(where)
        label_clause = f":{_ident(label)}" if label else ""
        rows = self._run(
            f"MATCH (n{label_clause}) WHERE {clause} RETURN count(n) AS c", **params
        )
        return int(rows[0]["c"]) if rows else 0

    def count_edges(self, rel_type: str | None = None) -> int:
        pattern = f"[r:{_ident(rel_type)}]" if rel_type else "[r]"
        rows = self._run(f"MATCH ()-{pattern}->() RETURN count(r) AS c")
        return int(rows[0]["c"]) if rows else 0

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
        rel_clause = (
            f":{'|'.join(_ident(t) for t in rel_types)}" if rel_types else ""
        )
        left, right = ("-", "->") if direction == "out" else (
            ("<-", "-") if direction == "in" else ("-", "-")
        )
        target_clause = f":{_ident(target_label)}" if target_label else ""
        where, params = _compile_where(target_where, alias="m")
        cypher = (
            f"MATCH (n){left}[r{rel_clause}]{right}(m{target_clause}) "
            f"WHERE n.id IN $ids AND {where} "
            "RETURN n, r, m"
        )
        if limit is not None:
            cypher += f" LIMIT {int(limit)}"
        rows = self._run(cypher, ids=node_ids, **params)
        out: list[tuple[Node, Edge, Node]] = []
        for row in rows:
            anchor = _to_node(row["n"])
            other = _to_node(row["m"])
            edge = _to_edge(row["r"])
            out.append((anchor, edge, other))
        return out

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
        source_ids = list(dict.fromkeys(source_ids))
        if not source_ids:
            return []
        # Availability is settled by the store before the transaction opens.
        # Calling a missing procedure here would abort the whole transaction,
        # taking the fallback down with it -- the failure mode this replaced.
        if self._supports_algo:
            return self._paths_via_algo(
                source_ids,
                rel_types,
                direction,
                max_len,
                path_count,
                result_limit,
                target_labels,
            )
        return self._paths_via_match(
            source_ids, rel_types, direction, max_len, result_limit, target_labels
        )

    def _paths_via_algo(
        self,
        source_ids: Sequence[str],
        rel_types: Sequence[str] | None,
        direction: Direction,
        max_len: int,
        path_count: int,
        result_limit: int,
        target_labels: Sequence[str] | None,
    ) -> list[Path]:
        """Native bounded traversal via ``algo.MSpaths`` (batched by label)."""
        by_label: dict[str, list[str]] = {}
        rows = self._run(
            "MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, labels(n) AS labels",
            ids=list(source_ids),
        )
        for row in rows:
            labels = list(row["labels"]) or ["Node"]
            by_label.setdefault(labels[0], []).append(row["id"])

        collected: list[Path] = []
        for label, ids in by_label.items():
            if len(collected) >= result_limit:
                break
            result = self._run(
                "CALL algo.MSpaths({"
                "  sourceLabel: $label,"
                "  sourceProperty: 'id',"
                "  sourceValues: $values,"
                "  pairwise: false,"
                "  relTypes: $relTypes,"
                "  relDirection: $relDirection,"
                "  maxLen: $maxLen,"
                "  pathCount: $pathCount,"
                "  resultLimit: $resultLimit"
                "}) YIELD path RETURN path",
                label=label,
                values=ids,
                relTypes=list(rel_types) if rel_types else [],
                relDirection=_DIRECTION_TO_ALGO.get(direction, "both"),
                maxLen=max_len,
                pathCount=path_count,
                resultLimit=result_limit - len(collected),
            )
            for row in result:
                path = _to_path(row["path"])
                if target_labels and path.end.labels[0] not in set(target_labels):
                    continue
                collected.append(path)
        return collected[:result_limit]

    def _paths_via_match(
        self,
        source_ids: Sequence[str],
        rel_types: Sequence[str] | None,
        direction: Direction,
        max_len: int,
        result_limit: int,
        target_labels: Sequence[str] | None,
    ) -> list[Path]:
        rel_clause = f":{'|'.join(_ident(t) for t in rel_types)}" if rel_types else ""
        left, right = ("-", "->") if direction == "out" else (
            ("<-", "-") if direction == "in" else ("-", "-")
        )
        target_clause = (
            f":{_ident(target_labels[0])}"
            if target_labels and len(target_labels) == 1
            else ""
        )
        cypher = (
            f"MATCH p = (n){left}[r{rel_clause}*1..{int(max_len)}]{right}"
            f"(m{target_clause}) "
            "WHERE n.id IN $ids RETURN p LIMIT $limit"
        )
        rows = self._run(cypher, ids=list(source_ids), limit=int(result_limit))
        paths = [_to_path(row["p"]) for row in rows]
        if target_labels and not target_clause:
            wanted = set(target_labels)
            paths = [p for p in paths if p.end.labels[0] in wanted]
        return paths


class HydraGraphStore(GraphStore):
    """HydraDB over the Bolt protocol."""

    name = "hydra"

    def __init__(
        self,
        uri: str = "neo4j://localhost:7687",
        auth_token: str = "local-development-token-32-bytes",
        database: str | None = None,
        consistency: str = "causal",
    ) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "the 'neo4j' driver is required for the HydraDB backend: "
                "pip install 'neo4j>=5.18'"
            ) from exc
        self.uri = uri
        self.database = database
        self.consistency = consistency
        self.driver = GraphDatabase.driver(uri, auth=("", auth_token))
        self._supports_algo: bool | None = None
        # Carries causality forward between sessions when consistency=strong.
        self._bookmarks: Any = None

    @property
    def supports_algo(self) -> bool:
        """Whether this server exposes HydraDB's native path procedures.

        Probed once, in a throwaway session, because the only portable test is
        to call the procedure -- and a failed call aborts whatever transaction
        it runs in.
        """
        if self._supports_algo is None:
            self._supports_algo = self._probe_algo()
            log.info(
                "native path procedures %s; using %s for multi-hop traversal",
                "available" if self._supports_algo else "unavailable",
                "algo.MSpaths" if self._supports_algo else "variable-length MATCH",
            )
        return self._supports_algo

    def _probe_algo(self) -> bool:
        kwargs = {"database": self.database} if self.database else {}
        try:
            with self.driver.session(**kwargs) as session:
                session.run(
                    "CALL algo.MSpaths({"
                    "  sourceLabel: '__weave_probe', sourceProperty: 'id',"
                    "  sourceValues: [], pairwise: false, relTypes: [],"
                    "  relDirection: 'both', maxLen: 1, pathCount: 1, resultLimit: 1"
                    "}) YIELD path RETURN path LIMIT 1"
                ).consume()
            return True
        except Exception:
            return False

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        """Open a unit of work, honouring the configured consistency mode.

        Specification §8.3 asks for default causal consistency on ingestion and
        strong consistency for reads that must observe the latest
        consolidation. Over the HTTP API that is a ``consistency: strong``
        header; over Bolt the equivalent is bookmark chaining -- each committed
        transaction hands its bookmark to the next session, so a read is
        guaranteed to see every write this client has already made, even if the
        cluster routes it to a different member.

        Under ``causal`` (the default) sessions are not chained: each is
        internally consistent, which is all ingestion needs and costs less.
        """
        kwargs: dict[str, Any] = {}
        if self.database:
            kwargs["database"] = self.database
        strong = self.consistency == "strong"
        if strong and self._bookmarks is not None:
            kwargs["bookmarks"] = self._bookmarks

        supports_algo = self.supports_algo
        with self.driver.session(**kwargs) as session:
            tx = session.begin_transaction()
            try:
                yield HydraTx(tx, supports_algo=supports_algo)
                tx.commit()
                if strong:
                    self._bookmarks = session.last_bookmarks()
            except Exception:
                tx.rollback()
                raise

    def ensure_schema(self) -> list[str]:
        applied: list[str] = []
        for statement in schema.cypher_index_statements():
            try:
                with self.driver.session() as session:
                    session.run(statement)
                applied.append(statement)
            except Exception:
                # Index already present, or this build spells the DDL differently.
                continue
        return applied

    def verify(self) -> bool:
        with self.driver.session() as session:
            return session.run("RETURN 1 AS n").single()["n"] == 1

    def stats(self) -> dict[str, Any]:
        with self.driver.session() as session:
            labels = {
                row["label"]: row["c"]
                for row in session.run(
                    "MATCH (n) UNWIND labels(n) AS label "
                    "RETURN label, count(*) AS c"
                )
            }
            rels = {
                row["type"]: row["c"]
                for row in session.run(
                    "MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS c"
                )
            }
        layers = {"episodic": 0, "semantic": 0, "procedural": 0}
        for label, count in labels.items():
            layer = schema.LAYER_OF_LABEL.get(label)
            if layer:
                layers[layer] += count
        return {
            "backend": self.name,
            "location": self.uri,
            "nodes": sum(labels.values()),
            "edges": sum(rels.values()),
            "by_label": labels,
            "by_relationship": rels,
            "by_layer": layers,
        }

    def reset(self) -> None:
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    def close(self) -> None:
        self.driver.close()
