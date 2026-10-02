import pytest

from sqlite_utils import Database
from sqlite_utils.db import (
    ForeignKeyClosureConflictError,
    MissingForeignKeyReferenceError,
    TransactionError,
)

SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    referred_by INTEGER REFERENCES customers(id)
);
CREATE TABLE products (
    sku TEXT NOT NULL,
    variant TEXT NOT NULL,
    name TEXT,
    PRIMARY KEY (sku, variant)
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id)
);
CREATE TABLE order_items (
    order_id INTEGER NOT NULL,
    sku TEXT,
    variant TEXT,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (order_id, sku, variant),
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (sku, variant) REFERENCES products(sku, variant)
);
"""

DATA = """
INSERT INTO customers (id, name, referred_by) VALUES
    (1, 'Alice', 2),
    (2, 'Bob', 1),
    (3, 'Cara', NULL);
INSERT INTO products (sku, variant, name) VALUES
    ('pen', 'blue', 'Blue pen'),
    ('notebook', 'a5', 'A5 notebook');
INSERT INTO orders (id, customer_id) VALUES
    (10, 1),
    (11, 3);
INSERT INTO order_items (order_id, sku, variant, quantity) VALUES
    (10, 'pen', 'blue', 2),
    (10, 'notebook', 'a5', 1),
    (11, 'pen', 'blue', 4);
"""


@pytest.fixture
def source_db(tmp_path):
    db = Database(tmp_path / "source.db")
    db.executescript(SCHEMA)
    db.executescript(DATA)
    return db


@pytest.fixture
def target_db(tmp_path):
    db = Database(tmp_path / "target.db")
    db.executescript(SCHEMA)
    return db


@pytest.fixture
def empty_target_db(tmp_path):
    return Database(tmp_path / "empty-target.db")


def rows_by_table(db):
    return {
        "customers": list(db["customers"].rows_where(order_by="id")),
        "products": list(db["products"].rows_where(order_by="sku, variant")),
        "orders": list(db["orders"].rows_where(order_by="id")),
        "order_items": list(
            db["order_items"].rows_where(order_by="order_id, sku, variant")
        ),
    }


def test_copies_cyclic_self_reference_and_compound_foreign_keys(
    source_db, empty_target_db
):
    target_db = empty_target_db
    result = source_db["order_items"].copy_foreign_key_closure(
        target_db, [(10, "pen", "blue")]
    )

    assert rows_by_table(target_db) == {
        "customers": [
            {"id": 1, "name": "Alice", "referred_by": 2},
            {"id": 2, "name": "Bob", "referred_by": 1},
        ],
        "products": [{"sku": "pen", "variant": "blue", "name": "Blue pen"}],
        "orders": [{"id": 10, "customer_id": 1}],
        "order_items": [
            {"order_id": 10, "sku": "pen", "variant": "blue", "quantity": 2}
        ],
    }
    assert target_db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(result.rows) == 5
    assert len(result.copied) == 5
    assert result.reused == ()
    assert set(result.created_tables) == {
        "order_items",
        "orders",
        "products",
        "customers",
    }


def test_null_compound_foreign_key_adds_no_dependency(source_db, target_db):
    source_db["order_items"].insert(
        {"order_id": 10, "sku": None, "variant": None, "quantity": 1}, pk=(
            "order_id",
            "sku",
            "variant",
        )
    )

    source_db["order_items"].copy_foreign_key_closure(
        target_db, [(10, None, None)]
    )

    assert rows_by_table(target_db) == {
        "customers": [
            {"id": 1, "name": "Alice", "referred_by": 2},
            {"id": 2, "name": "Bob", "referred_by": 1},
        ],
        "products": [],
        "orders": [{"id": 10, "customer_id": 1}],
        "order_items": [
            {"order_id": 10, "sku": None, "variant": None, "quantity": 1}
        ],
    }
    assert target_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_same_row_reached_by_multiple_edges_is_inserted_once(source_db, target_db):
    result = source_db["order_items"].copy_foreign_key_closure(
        target_db,
        [(10, "pen", "blue"), (11, "pen", "blue")],
    )

    assert len(result.rows) == 8
    assert len(result.copied) == 8
    assert rows_by_table(target_db)["customers"] == [
        {"id": 1, "name": "Alice", "referred_by": 2},
        {"id": 2, "name": "Bob", "referred_by": 1},
        {"id": 3, "name": "Cara", "referred_by": None},
    ]
    assert rows_by_table(target_db)["products"] == [
        {"sku": "pen", "variant": "blue", "name": "Blue pen"}
    ]
    assert rows_by_table(target_db)["orders"] == [
        {"id": 10, "customer_id": 1},
        {"id": 11, "customer_id": 3},
    ]
    assert target_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_existing_identical_rows_are_reused(source_db, target_db):
    target_db["customers"].insert(
        {"id": 2, "name": "Bob", "referred_by": 1}, pk="id"
    )

    result = source_db["customers"].copy_foreign_key_closure(target_db, 1)

    assert len(result.copied) == 1
    assert [(row.table, row.primary_key) for row in result.reused] == [
        ("customers", (2,))
    ]
    assert rows_by_table(target_db)["customers"] == [
        {"id": 1, "name": "Alice", "referred_by": 2},
        {"id": 2, "name": "Bob", "referred_by": 1},
    ]
    assert target_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_conflicting_existing_row_rolls_back_entire_import(source_db, target_db):
    target_db["customers"].insert(
        {"id": 2, "name": "Original Bob", "referred_by": None}, pk="id"
    )
    before = rows_by_table(target_db)

    with pytest.raises(ForeignKeyClosureConflictError) as exc_info:
        source_db["customers"].copy_foreign_key_closure(target_db, 1)

    conflict = exc_info.value.conflicts[0]
    assert conflict.table == "customers"
    assert conflict.primary_key == (2,)
    assert conflict.differences == {
        "name": ("Bob", "Original Bob"),
        "referred_by": (1, None),
    }
    assert rows_by_table(target_db) == before
    assert target_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_conflict_rolls_back_inside_existing_transaction(source_db, target_db):
    target_db.execute("PRAGMA foreign_keys=ON")
    target_db["customers"].insert(
        {"id": 2, "name": "Original Bob", "referred_by": None}, pk="id"
    )
    target_db.commit()
    target_db.begin()
    target_db["products"].insert(
        {"sku": "kept", "variant": "x", "name": "Kept"},
        pk=("sku", "variant"),
    )

    with pytest.raises(ForeignKeyClosureConflictError):
        source_db["customers"].copy_foreign_key_closure(target_db, 1)

    target_db.rollback()
    assert list(target_db["products"].rows_where()) == []
    assert target_db["customers"].get(2)["name"] == "Original Bob"


def test_existing_transaction_requires_foreign_keys_to_be_enabled(
    source_db, target_db
):
    target_db.begin()
    with pytest.raises(TransactionError):
        source_db["customers"].copy_foreign_key_closure(target_db, 1)
    target_db.rollback()


def test_missing_foreign_key_reference_reports_error_and_writes_nothing(
    source_db, target_db
):
    source_db.execute("DELETE FROM products WHERE sku = 'pen'")
    before = rows_by_table(target_db)

    with pytest.raises(MissingForeignKeyReferenceError) as exc_info:
        source_db["order_items"].copy_foreign_key_closure(
            target_db, [(10, "pen", "blue")]
        )

    reference = exc_info.value.references[0]
    assert reference.table == "order_items"
    assert reference.key == ("pen", "blue")
    assert reference.other_table == "products"
    assert rows_by_table(target_db) == before
    assert target_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_preserves_rowid_for_tables_without_primary_keys(tmp_path):
    source = Database(tmp_path / "rowid-source.db")
    target = Database(tmp_path / "rowid-target.db")
    source.executescript(
        """
        CREATE TABLE parents (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE children (name TEXT, parent_id INTEGER REFERENCES parents(id));
        INSERT INTO parents (id, name) VALUES (5, 'parent');
        INSERT INTO children (rowid, name, parent_id) VALUES (42, 'child', 5);
        """
    )

    result = source["children"].copy_foreign_key_closure(target, 42)

    assert target_db_rows(target) == [
        {"rowid": 42, "name": "child", "parent_id": 5}
    ]
    assert [(row.table, row.primary_key) for row in result.rows] == [
        ("children", (42,)),
        ("parents", (5,)),
    ]
    assert target.execute("PRAGMA foreign_key_check").fetchall() == []


def target_db_rows(db):
    return list(db["children"].rows_where(select="rowid, name, parent_id"))
