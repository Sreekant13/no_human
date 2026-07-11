"""2.2 Stacked-PR ordered merge — pure topology over a PR dependency DAG.

An edge ``(child, parent)`` means *parent must merge before child* (else the
child merges code that references an un-merged base). This module computes the
safe merge ORDER and which PRs are ready right now. The agent never merges
(CLAUDE.md #2) — an operator drives the actual merges (``nh merge-stack``) using
this order. Pure functions so the topology is unit-pinned.
"""

from __future__ import annotations

from typing import Iterable


class MergeCycle(ValueError):
    """The PR dependency graph has a cycle — no safe merge order exists."""


def _nodes_and_parents(edges: Iterable[tuple[str, str]]):
    parents_of: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for child, parent in edges:
        nodes.add(child)
        nodes.add(parent)
        parents_of.setdefault(child, set()).add(parent)
        parents_of.setdefault(parent, set())
    return nodes, parents_of


def merge_order(edges: Iterable[tuple[str, str]]) -> list[str]:
    """Topological order: every parent appears before its children. Raises
    :class:`MergeCycle` on a cycle. Deterministic (sorted) for stable output."""
    nodes, parents_of = _nodes_and_parents(edges)
    order: list[str] = []
    done: set[str] = set()
    while len(done) < len(nodes):
        ready = sorted(
            n for n in nodes
            if n not in done and parents_of.get(n, set()) <= done
        )
        if not ready:
            raise MergeCycle(f"cycle among PRs: {sorted(nodes - done)}")
        order.extend(ready)
        done.update(ready)
    return order


def ready_to_merge(
    edges: Iterable[tuple[str, str]], merged: Iterable[str],
) -> list[str]:
    """The PRs whose parents are ALL already merged and which are not themselves
    merged — the set safe to merge in the current state."""
    merged_set = set(merged)
    nodes, parents_of = _nodes_and_parents(edges)
    return sorted(
        n for n in nodes
        if n not in merged_set and parents_of.get(n, set()) <= merged_set
    )
