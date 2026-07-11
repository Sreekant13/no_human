"""2.2 stacked-PR ordered merge — the topology must be exactly right, or a
child PR merges code that references an un-merged base."""

import pytest

from no_human.vcs.merge_order import MergeCycle, merge_order, ready_to_merge


def test_linear_chain_orders_parents_first():
    # C depends on B depends on A
    edges = [("C", "B"), ("B", "A")]
    assert merge_order(edges) == ["A", "B", "C"]


def test_diamond_orders_base_before_branches_before_join():
    # D depends on B and C; both depend on A
    edges = [("B", "A"), ("C", "A"), ("D", "B"), ("D", "C")]
    order = merge_order(edges)
    assert order.index("A") < order.index("B") < order.index("D")
    assert order.index("A") < order.index("C") < order.index("D")


def test_cycle_is_rejected():
    with pytest.raises(MergeCycle):
        merge_order([("A", "B"), ("B", "A")])


def test_ready_to_merge_advances_as_parents_merge():
    edges = [("C", "B"), ("B", "A")]
    assert ready_to_merge(edges, merged=set()) == ["A"]
    assert ready_to_merge(edges, merged={"A"}) == ["B"]
    assert ready_to_merge(edges, merged={"A", "B"}) == ["C"]
    assert ready_to_merge(edges, merged={"A", "B", "C"}) == []


def test_ready_returns_all_independently_unblocked():
    # two independent chains → both roots ready at once
    edges = [("B", "A"), ("Y", "X")]
    assert ready_to_merge(edges, merged=set()) == ["A", "X"]
