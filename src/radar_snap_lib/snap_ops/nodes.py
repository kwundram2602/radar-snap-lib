"""The node primitives.

These live apart from :mod:`radar_snap_lib.snap_ops.graph` to keep the import
graph acyclic: the generated builder mixin needs :class:`NodeRef` for its
annotations, and ``graph`` needs the mixin as a base class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Node", "NodeRef"]


@dataclass(frozen=True)
class NodeRef:
    """Handle to a node in a graph, used to wire up sources."""

    id: str

    def __str__(self) -> str:
        return self.id


@dataclass
class Node:
    """A single operator invocation."""

    id: str
    op: str
    sources: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> NodeRef:
        return NodeRef(self.id)
