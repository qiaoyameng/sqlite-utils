from sqlite_utils import Database
from sqlite_utils.db import (
    ForeignKeyClosureConflict,
    MissingForeignKeyReference,
)
from sqlite_utils.utils import sqlite3


SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    manager_id INTEGER REFERENCES customers(id)
);
CREATE TABLE products (
    product_id INTEGER,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (product_id, sku)
);
CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    parent_order_id TEXT REFERENCES orders(id)
);
CREATE TABLE order_items (
    order_id TEXT NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (order_id, product_id, sku),
    FOREIGN KEY (product_id, sku) REFERENCES products(product_id, sku)
);
"""


def databases(tmp_path):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "destination.db"
    for path in (source_path, destination_path):
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        connection.commit()
        connection.close()
    return Database(source_path), Database(destination_path)


def all_rows(db):
    return {
        table: db.execute(
            f"SELECT * FROM {table} ORDER BY {', '.join(map(str, range(1, column_count + 1)))}"
        ).fetchall()
        for table, column_count in (
            ("customers", 3),
            ("products", 3),
            ("orders", 3),
            ("order_items", 4),
        )
    }


def test_copies_circular_and_compound_foreign_key_closure(tmp_path):
    source, destination = databases(tmp_path)
    source.executescript(
        """
        INSERT INTO customers (id, name, manager_id) VALUES
            (1, 'Ada', 2),
            (2, 'Grace', 1),
            (3, 'No dependency', NULL);
        INSERT INTO products (product_id, sku, name) VALUES
            (10, 'A', 'Widget'),
            (10, 'B', 'Gadget'),
            (11, 'C', 'Do not copy');
        INSERT INTO orders (id, customer_id, parent_order_id) VALUES
            ('o1', 1, NULL),
            ('o0', 3, NULL);
        INSERT INTO order_items (order_id, product_id, sku, quantity) VALUES
            ('o1', 10, 'A', 2),
            ('o1', 10, 'B', 1),
            ('o0', 11, 'C', 1);
        """
    )

    result = source.copy_foreign_key_closure(
        destination,
        "order_items",
        [("o1", 10, "A"), ("o1", 10, "B")],
    )

    assert result.inserted == {
        "customers": [(1,), (2,)],
        "products": [(10, "A"), (10, "B")],
        "orders": [("o1",)],
        "order_items": [("o1", 10, "A"), ("o1", 10, "B")],
    }
    assert result.reused == {}
    assert all_rows(destination) == {
        "customers": [(1, "Ada", 2), (2, "Grace", 1)],
        "products": [(10, "A", "Widget"), (10, "B", "Gadget")],
        "orders": [("o1", 1, None)],
        "order_items": [("o1", 10, "A", 2), ("o1", 10, "B", 1)],
    }
    assert destination.execute("PRAGMA foreign_key_check").fetchall() == []


def test_same_database_cycle_restores_foreign_key_pragma(tmp_path):
    source_path = tmp_path / "same.db"
    connection = sqlite3.connect(source_path)
    connection.execute(
        "CREATE TABLE cycle (id INTEGER PRIMARY KEY, next_id INTEGER REFERENCES cycle(id))"
    )
    connection.commit()
    connection.close()

    db = Database(source_path)
    db.execute("INSERT INTO cycle (id, next_id) VALUES (1, 2)")
    db.execute("INSERT INTO cycle (id, next_id) VALUES (2, 1)")
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 0

    db.copy_foreign_key_closure(db, "cycle", 1)

    assert db.execute("SELECT COUNT(*) FROM cycle").fetchone()[0] == 2
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 0


def test_reuses_identical_existing_rows(tmp_path):
    source, destination = databases(tmp_path)
    source.execute(
        "INSERT INTO customers (id, name, manager_id) VALUES (1, 'Ada', NULL)"
    )
    source.execute(
        "INSERT INTO orders (id, customer_id, parent_order_id) VALUES ('o1', 1, NULL)"
    )
    destination.execute(
        "INSERT INTO customers (id, name, manager_id) VALUES (1, 'Ada', NULL)"
    )

    result = source.copy_foreign_key_closure(destination, "orders", "o1")

    assert result.inserted == {"orders": [("o1",)]}
    assert result.reused == {"customers": [(1,)]}
    assert destination.execute("PRAGMA foreign_key_check").fetchall() == []


def test_conflicting_existing_row_reports_values_and_rolls_back(tmp_path):
    source, destination = databases(tmp_path)
    source.execute(
        "INSERT INTO customers (id, name, manager_id) VALUES (1, 'Ada', NULL)"
    )
    source.execute(
        "INSERT INTO orders (id, customer_id, parent_order_id) VALUES ('o1', 1, NULL)"
    )
    destination.execute(
        "INSERT INTO customers (id, name, manager_id) VALUES (1, 'Different', NULL)"
    )
    before = all_rows(destination)

    try:
        source.copy_foreign_key_closure(destination, "orders", "o1")
    except ForeignKeyClosureConflict as error:
        assert error.conflicts[0].table == "customers"
        assert error.conflicts[0].key == (1,)
        assert error.conflicts[0].differences == ("name",)
        assert error.conflicts[0].source_values["name"] == "Ada"
        assert error.conflicts[0].destination_values["name"] == "Different"
    else:
        raise AssertionError("Expected ForeignKeyClosureConflict")

    assert all_rows(destination) == before
    assert destination.execute("PRAGMA foreign_key_check").fetchall() == []


def test_null_foreign_key_adds_no_dependency(tmp_path):
    source, destination = databases(tmp_path)
    source.execute(
        "INSERT INTO orders (id, customer_id, parent_order_id) VALUES ('orphan', 1, NULL)"
    )
    source.execute(
        "INSERT INTO customers (id, name, manager_id) VALUES (1, 'Ada', NULL)"
    )

    source.copy_foreign_key_closure(destination, "customers", 1)

    assert all_rows(destination)["orders"] == []
    assert destination.execute("PRAGMA foreign_key_check").fetchall() == []


def test_missing_compound_foreign_key_reference_rolls_back(tmp_path):
    source, destination = databases(tmp_path)
    source.execute(
        "INSERT INTO customers (id, name, manager_id) VALUES (1, 'Ada', NULL)"
    )
    source.execute(
        "INSERT INTO orders (id, customer_id, parent_order_id) VALUES ('o1', 1, NULL)"
    )
    # The products row is deliberately absent.
    source.execute(
        "INSERT INTO order_items (order_id, product_id, sku, quantity) "
        "VALUES ('o1', 99, 'MISSING', 1)"
    )
    destination.execute(
        "INSERT INTO customers (id, name, manager_id) VALUES (99, 'Keep me', NULL)"
    )
    before = all_rows(destination)

    try:
        source.copy_foreign_key_closure(
            destination,
            "order_items",
            [("o1", 99, "MISSING")],
        )
    except MissingForeignKeyReference as error:
        reference = error.references[0]
        assert reference.table == "order_items"
        assert reference.row_key == ("o1", 99, "MISSING")
        assert reference.other_table == "products"
        assert reference.key_values == (99, "MISSING")
    else:
        raise AssertionError("Expected MissingForeignKeyReference")

    assert all_rows(destination) == before
    assert destination.execute("PRAGMA foreign_key_check").fetchall() == []
