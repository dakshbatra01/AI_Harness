import pytest

from inventory import InsufficientStock, Inventory
from inventory.report import summary


def make():
    inv = Inventory()
    inv.add_item("A1", "widget", 10)
    return inv


def test_new_items_have_nothing_reserved():
    item = make().get("A1")
    assert item.reserved == 0
    assert item.available == 10


def test_reserve_reduces_available_not_on_hand():
    inv = make()
    inv.reserve("A1", 3)
    item = inv.get("A1")
    assert (item.on_hand, item.reserved, item.available) == (10, 3, 7)


def test_cannot_reserve_more_than_available():
    inv = make()
    inv.reserve("A1", 8)
    with pytest.raises(InsufficientStock):
        inv.reserve("A1", 3)


def test_release():
    inv = make()
    inv.reserve("A1", 4)
    inv.release("A1", 3)
    assert inv.get("A1").reserved == 1
    with pytest.raises(ValueError):
        inv.release("A1", 2)


def test_ship_only_from_available_stock():
    inv = make()
    inv.reserve("A1", 6)
    with pytest.raises(InsufficientStock):
        inv.ship("A1", 5)
    inv.ship("A1", 4)
    assert (inv.get("A1").on_hand, inv.get("A1").available) == (6, 0)


def test_summary_total_line():
    inv = make()
    inv.add_item("B2", "bolt", 5)
    inv.reserve("A1", 3)
    total_line = summary(inv).splitlines()[-1]
    assert "on_hand=15" in total_line
    assert "reserved=3" in total_line
    assert "available=12" in total_line
