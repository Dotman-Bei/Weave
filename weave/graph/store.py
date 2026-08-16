"""The graph substrate contract that every Weave backend implements.

Weave is graph-native: the services never speak SQL, dicts, or vectors -- they
speak nodes, edges, labelled traversal and bounded paths. That contract lives
here so the embedded engine and the HydraDB/Bolt engine stay interchangeable.
"""

from __future__ import annotations

import abc
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

Props = dict[str, Any]

# Contract note on null properties
# --------------------------------
# Setting a property to ``None`` is *not* stored identically by every backend.
# The embedded engine keeps the key with a null value; OpenCypher's
# ``SET n.x = null`` removes the key outright. Both are correct -- an
# ``is_null()`` filter matches either -- but it means a nullable property may
# be absent rather than null on read.
#
# So: always read a nullable property with ``node.get(key)``, never
# ``node.props[key]``. Only properties that are always written may be indexed
# directly.

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    id: str
    labels: tuple[str, ...]
    props: Props

    def __getitem__(self, key: str) -> Any:
        return self.props[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.props.get(key, default)

    def has_label(self, label: str) -> bool:
        return label in self.labels


@dataclass(frozen=True)
class Edge:
    id: str
    type: str
    start_id: str
    end_id: str
    props: Props

    def get(self, key: str, default: Any = None) -> Any:
        return self.props.get(key, default)


@dataclass(frozen=True)
class Path:
    """An alternating node/edge walk. ``nodes[i] -edges[i]-> nodes[i + 1]``."""

    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]

    @property
    def length(self) -> int:
        return len(self.edges)

    @property
    def start(self) -> Node:
        return self.nodes[0]

    @property
    def end(self) -> Node:
        return self.nodes[-1]


# ---------------------------------------------------------------------------
# Filter DSL
#
# A filter is a mapping of property name -> value (equality) or property name
# -> Cmp (explicit operator). Both backends compile it: the embedded engine to
# SQLite json_extract predicates, the Hydra engine to OpenCypher WHERE clauses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cmp:
    op: str
    value: Any = None


def eq(value: Any) -> Cmp:
    return Cmp("=", value)


def ne(value: Any) -> Cmp:
    return Cmp("<>", value)


def in_(values: Sequence[Any]) -> Cmp:
    return Cmp("IN", list(values))


def gt(value: Any) -> Cmp:
    return Cmp(">", value)


def gte(value: Any) -> Cmp:
    return Cmp(">=", value)


def lt(value: Any) -> Cmp:
    return Cmp("<", value)


def lte(value: Any) -> Cmp:
    return Cmp("<=", value)


def is_null() -> Cmp:
    return Cmp("IS NULL")


def not_null() -> Cmp:
    return Cmp("IS NOT NULL")


def contains(value: str) -> Cmp:
    """Case-insensitive substring match, on every backend."""
    return Cmp("CONTAINS", value)


Filter = Mapping[str, Any]
OrderBy = Sequence[tuple[str, str]]  # [(property, "asc" | "desc"), ...]
Direction = str  # "out" | "in" | "both"


def normalise_filter(where: Filter | None) -> list[tuple[str, Cmp]]:
    """Normalise bare values into explicit equality comparisons."""
    if not where:
        return []
    out: list[tuple[str, Cmp]] = []
    for key, value in where.items():
        out.append((key, value if isinstance(value, Cmp) else Cmp("=", value)))
    return out


# ---------------------------------------------------------------------------
# Transaction / store contracts
# ---------------------------------------------------------------------------


class Tx(abc.ABC):
    """A unit of work. Every mutation in Weave happens inside one."""

    # -- writes ------------------------------------------------------------

    @abc.abstractmethod
    def create_node(self, labels: Sequence[str], props: Props) -> Node:
        """Create a node. ``props['id']`` is generated when absent."""

    @abc.abstractmethod
    def merge_node(
        self,
        label: str,
        key: Props,
        on_create: Props | None = None,
        on_match: Props | None = None,
    ) -> tuple[Node, bool]:
        """MERGE semantics. Returns ``(node, was_created)``."""

    @abc.abstractmethod
    def set_props(self, node_id: str, props: Props) -> Node:
        """Set properties on an existing node, returning the updated node."""

    @abc.abstractmethod
    def create_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> Edge:
        """Create a relationship."""

    @abc.abstractmethod
    def merge_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> tuple[Edge, bool]:
        """Create the relationship only if an identical one does not exist."""

    @abc.abstractmethod
    def upsert_edge(
        self, start_id: str, end_id: str, rel_type: str, props: Props | None = None
    ) -> Edge:
        """Create the relationship, or replace the properties of the existing one.

        Distinct from :meth:`create_edge`, which is free to add a parallel
        relationship. Anything that carries running state on an edge -- a
        success rate, an attempt count -- must use this, or repeated writes
        accumulate duplicate edges and the state stops being readable.
        """

    # -- reads -------------------------------------------------------------

    @abc.abstractmethod
    def get_node(self, node_id: str) -> Node | None:
        ...

    @abc.abstractmethod
    def match(
        self,
        label: str | None = None,
        where: Filter | None = None,
        order_by: OrderBy | None = None,
        limit: int | None = None,
        skip: int | None = None,
    ) -> list[Node]:
        """Label + property scan, index-assisted where possible."""

    @abc.abstractmethod
    def count(self, label: str | None = None, where: Filter | None = None) -> int:
        ...

    @abc.abstractmethod
    def expand(
        self,
        node_ids: Sequence[str],
        rel_types: Sequence[str] | None = None,
        direction: Direction = "both",
        target_label: str | None = None,
        target_where: Filter | None = None,
        limit: int | None = None,
    ) -> list[tuple[Node, Edge, Node]]:
        """One hop. Returns ``(anchor, edge, neighbour)`` triples."""

    @abc.abstractmethod
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
        """Bounded multi-hop traversal.

        This is the primitive that maps onto HydraDB's ``algo.SSpaths`` /
        ``algo.MSpaths`` procedures, and onto a capped BFS in the embedded
        engine. ``path_count`` bounds paths per source; ``result_limit`` bounds
        the total returned.
        """

    def search_text(
        self, label: str, prop: str, terms: Sequence[str], limit: int = 60
    ) -> list[Node]:
        """Nodes whose ``prop`` contains any of ``terms``, best-effort ranked.

        Not abstract: the default satisfies the contract with the substring
        filter every backend already implements, so a backend only overrides
        this if it has a real text index to offer. Callers get the same answers
        either way -- only the cost differs.
        """
        found: dict[str, Node] = {}
        for term in terms:
            for node in self.match(label, {prop: contains(term)}, limit=limit):
                found[node.id] = node
        return list(found.values())[: limit * max(1, len(list(terms)))]

    @abc.abstractmethod
    def count_edges(self, rel_type: str | None = None) -> int:
        ...


class GraphStore(abc.ABC):
    """A property-graph backend."""

    name: str = "graph"

    @contextmanager
    def transaction(self) -> Iterator[Tx]:  # pragma: no cover - overridden
        raise NotImplementedError

    @abc.abstractmethod
    def ensure_schema(self) -> list[str]:
        """Create indexes. Returns the list of index statements applied."""

    @abc.abstractmethod
    def verify(self) -> bool:
        """Round-trip check that the store is alive."""

    @abc.abstractmethod
    def stats(self) -> dict[str, Any]:
        """Per-label / per-relationship counts for the health endpoint."""

    @abc.abstractmethod
    def reset(self) -> None:
        """Drop all data. Used by tests and the demo reset button."""

    def close(self) -> None:
        return None
