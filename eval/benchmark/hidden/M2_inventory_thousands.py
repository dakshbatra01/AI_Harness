from inventory.importer import load_csv
from inventory.parsing import parse_quantity
from inventory.report import summary


def test_thousands_separators():
    assert parse_quantity("1,200") == 1200
    assert parse_quantity("12,345,678") == 12345678


def test_plain_numbers_still_work():
    assert parse_quantity(" 40 ") == 40
    assert parse_quantity("7") == 7


def test_csv_import_with_quoted_thousands(tmp_path):
    p = tmp_path / "stock.csv"
    p.write_text('sku,name,quantity\nA1,anchor,"1,200"\nB2,bolt,40\n')
    inv = load_csv(p)
    assert inv.get("A1").on_hand == 1200
    assert "on_hand=1240" in summary(inv)
