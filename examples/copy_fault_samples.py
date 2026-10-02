"""
Demonstrate copying a foreign-key dependency closure between SQLite files.

The script creates all databases in a temporary directory and removes that
directory on exit.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlite_utils import Database
from sqlite_utils.db import (
    ForeignKeyClosureConflictError,
    MissingForeignKeyReferenceError,
)

SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT,
    referred_by INTEGER REFERENCES customers(id)
);
CREATE TABLE products (
    sku TEXT,
    variant TEXT,
    name TEXT,
    PRIMARY KEY (sku, variant)
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id)
);
CREATE TABLE order_items (
    order_id INTEGER,
    sku TEXT,
    variant TEXT,
    quantity INTEGER,
    PRIMARY KEY (order_id, sku, variant),
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (sku, variant) REFERENCES products(sku, variant)
);
"""

DATA = """
INSERT INTO customers VALUES
    (1, 'Alice', 2),
    (2, 'Bob', 1),
    (3, 'Cara', NULL);
INSERT INTO products VALUES ('pen', 'blue', 'Blue pen');
INSERT INTO orders VALUES (10, 1), (11, 3);
INSERT INTO order_items VALUES
    (10, 'pen', 'blue', 2),
    (11, 'missing', 'red', 1);
"""


def snapshot(db: Database) -> dict[str, list[dict]]:
    return {
        table: list(db[table].rows_where(order_by="1"))
        for table in ("customers", "products", "orders", "order_items")
    }


def print_snapshot(label: str, db: Database) -> None:
    print(label)
    for table, rows in snapshot(db).items():
        print(f"  {table}: {rows}")
    print("  foreign_key_check:", db.execute("PRAGMA foreign_key_check").fetchall())


with tempfile.TemporaryDirectory() as directory:
    source_path = Path(directory) / "source.db"
    target_path = Path(directory) / "target.db"
    source = Database(source_path)
    target = Database(target_path)

    source.executescript(SCHEMA)
    source.executescript(DATA)

    result = source["order_items"].copy_foreign_key_closure(
        target, [(10, "pen", "blue")]
    )
    print_snapshot(
        f"Copied {len(result.copied)} rows from {source_path} to {target_path}:",
        target,
    )

    # The rows copied above are reused by a second, overlapping import.
    reused_result = source["customers"].copy_foreign_key_closure(target, 1)
    print("Reused rows:", [(row.table, row.primary_key) for row in reused_result.reused])

    # Same key with different values reports concrete conflicts and rolls back.
    target["customers"].update(2, {"name": "Original Bob", "referred_by": None})
    before_conflict = snapshot(target)
    try:
        source["customers"].copy_foreign_key_closure(target, 1)
    except ForeignKeyClosureConflictError as error:
        print("Conflict:", error.conflicts[0].differences)
    assert snapshot(target) == before_conflict
    print("Target rows remained unchanged after the conflict.")

    # A missing non-NULL compound dependency is reported without writes.
    target.executescript("DELETE FROM order_items; DELETE FROM orders;")
    target.executescript("DELETE FROM products; DELETE FROM customers;")
    before_missing = snapshot(target)
    try:
        source["order_items"].copy_foreign_key_closure(
            target, [(11, "missing", "red")]
        )
    except MissingForeignKeyReferenceError as error:
        reference = error.references
        print("Missing references:", reference)
    assert snapshot(target) == before_missing
    print_snapshot("Target after missing-reference import was rejected:", target)
