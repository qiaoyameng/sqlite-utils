import pytest

from sqlite_utils import Database
from sqlite_utils.db import ChangeLogGapError


def test_change_log_records_insert_update_and_delete(fresh_db):
    table = fresh_db.table("items")
    table.create({"id": int, "name": str, "payload": bytes}, pk="id")
    table.enable_changes()

    table.insert({"id": 1, "name": "one", "payload": b"old"})
    table.update(1, {"name": "ONE", "payload": b"new"})
    table.delete(1)

    batch = table.changes(0)
    assert batch.next_cursor == 3
    assert [change.op for change in batch.changes] == ["INSERT", "UPDATE", "DELETE"]
    assert batch.changes[0].new_pk == 1
    assert batch.changes[0].new_values == {
        "id": 1,
        "name": "one",
        "payload": b"old",
    }
    assert batch.changes[1].old_pk == batch.changes[1].new_pk == 1
    assert batch.changes[1].old_values["payload"] == b"old"
    assert batch.changes[1].new_values["payload"] == b"new"
    assert batch.changes[2].old_pk == 1
    assert batch.changes[2].old_values["id"] == 1
    assert batch.changes[2].new_values is None


def test_change_log_batches_resume_after_cursor(fresh_db):
    table = fresh_db.table("items")
    table.insert_all([{"id": value} for value in range(1, 6)], pk="id")
    table.enable_changes()
    table.insert_all([{"id": value} for value in range(6, 10)], pk="id")

    first = table.changes(0, limit=2)
    assert [change.new_pk for change in first.changes] == [6, 7]
    second = table.changes(first.next_cursor, limit=2)
    assert [change.new_pk for change in second.changes] == [8, 9]
    third = table.changes(second.next_cursor, limit=2)
    assert third.changes == []
    assert third.next_cursor == second.next_cursor


@pytest.mark.parametrize(
    "schema, pk, without_rowid",
    (
        ("id INTEGER PRIMARY KEY, name TEXT", "id", False),
        ("a TEXT, b TEXT, name TEXT, PRIMARY KEY(a, b)", ("a", "b"), False),
        ("a TEXT, b TEXT, name TEXT, PRIMARY KEY(a, b)", ("a", "b"), True),
    ),
)
def test_change_log_supports_compound_pks_without_rowid_and_pk_updates(
    fresh_db, schema, pk, without_rowid
):
    suffix = ") WITHOUT ROWID" if without_rowid else ")"
    fresh_db.execute(f"CREATE TABLE items ({schema}{suffix}")
    table = fresh_db.table("items")
    table.enable_changes()

    if isinstance(pk, tuple):
        table.insert({"a": "left", "b": "old", "name": "before"})
        table.update(("left", "old"), {"b": "new", "name": "after"})
        table.delete(("left", "new"))
        table.insert({"a": "left", "b": "old", "name": "again"})
        expected_old = ("left", "old")
        expected_new = ("left", "new")
    else:
        table.insert({"id": 1, "name": "before"})
        table.update(1, {"id": 2, "name": "after"})
        table.delete(2)
        table.insert({"id": 1, "name": "again"})
        expected_old, expected_new = 1, 2

    changes = table.changes(0, limit=10).changes
    assert (changes[1].old_pk, changes[1].new_pk) == (expected_old, expected_new)
    assert changes[2].op == "DELETE"
    assert changes[3].op == "INSERT"
    assert changes[3].new_pk == expected_old


def test_change_log_rolls_back_transactions_and_savepoints(fresh_db):
    table = fresh_db.table("items")
    table.create({"id": int, "name": str}, pk="id")
    table.enable_changes()

    with pytest.raises(RuntimeError), fresh_db.atomic():
        table.insert({"id": 1, "name": "rolled back"})
        raise RuntimeError

    with fresh_db.atomic():
        table.insert({"id": 2, "name": "committed"})
        with pytest.raises(RuntimeError), fresh_db.atomic():
            table.insert({"id": 3, "name": "savepoint rollback"})
            raise RuntimeError
        table.insert({"id": 4, "name": "after savepoint"})

    changes = table.changes(0, limit=10).changes
    assert [change.new_values["id"] for change in changes] == [2, 4]
    assert [change.seq for change in changes] == [1, 2]


def test_enable_disable_change_log_only_manages_its_own_triggers(fresh_db):
    table = fresh_db.table("items")
    table.insert({"id": 1}, pk="id")
    fresh_db.execute(
        "CREATE TRIGGER items_user AFTER INSERT ON items "
        "BEGIN UPDATE items SET id = id WHERE 0; END"
    )
    original_user_trigger = table.triggers_dict["items_user"]

    table.enable_changes()
    assert table.has_changes_triggers
    table.enable_changes()
    assert table.has_changes_triggers

    table.disable_changes()
    assert not table.has_changes_triggers
    assert table.triggers_dict["items_user"] == original_user_trigger


def test_change_log_acknowledgement_pruning_and_gap(fresh_db):
    table = fresh_db.table("items")
    table.insert_all([{"id": value} for value in range(1, 5)], pk="id")
    table.enable_changes()
    table.insert_all([{"id": value} for value in range(5, 8)], pk="id")

    batch = table.changes(0, limit=2)
    assert table.acknowledge_changes(batch.next_cursor) == batch.next_cursor
    assert table.change_log_watermark() == batch.next_cursor
    assert table.prune_changes() == 2
    assert [change.seq for change in table.changes(batch.next_cursor).changes] == [3]

    with pytest.raises(ChangeLogGapError):
        table.changes(0)
    assert table.changes(batch.next_cursor).changes
    with pytest.raises(ChangeLogGapError):
        table.acknowledge_changes(0)


def test_change_log_retention_is_per_table_and_consumer(fresh_db):
    one = fresh_db.table("one")
    two = fresh_db.table("two")
    one.create({"id": int}, pk="id")
    two.create({"id": int}, pk="id")
    one.enable_changes()
    two.enable_changes()
    fresh_db.acknowledge_changes(0, consumer="safe")
    one.insert({"id": 1})
    two.insert({"id": 2})

    one.acknowledge_changes(1, consumer="reader")
    two.acknowledge_changes(0, consumer="reader")
    assert one.prune_changes(consumer="reader") == 1
    assert one.changes(1, consumer="reader").changes == []
    assert [change.seq for change in two.changes(0, consumer="reader").changes] == [2]

    one.acknowledge_changes(2, consumer="safe")
    assert one.prune_changes(consumer="safe") == 0

    assert fresh_db.prune_changes(consumer="reader") == 0
    assert [change.seq for change in two.changes(0, consumer="reader").changes] == [2]


def test_change_log_visibility_across_two_connections(tmp_path):
    path = tmp_path / "changes.db"
    writer = Database(path)
    reader = Database(path)
    writer.table("items").create({"id": int}, pk="id")
    writer.table("items").enable_changes()

    writer.begin()
    writer.table("items").insert({"id": 1})
    assert reader.table("items").changes(0).changes == []

    writer.commit()
    first = reader.table("items").changes(0)
    assert [change.new_pk for change in first.changes] == [1]

    writer.begin()
    writer.table("items").insert({"id": 2})
    writer.rollback()
    assert reader.table("items").changes(first.next_cursor).changes == []

    writer.table("items").insert({"id": 3})
    second = reader.table("items").changes(first.next_cursor)
    assert [change.new_pk for change in second.changes] == [3]


def test_change_log_reads_its_index_without_scanning_the_business_table(
    fresh_db, monkeypatch
):
    table = fresh_db.table("items")
    table.insert_all([{"id": value} for value in range(1, 101)], pk="id")
    table.enable_changes()
    table.insert({"id": 101})

    queries = []
    monkeypatch.setattr(
        fresh_db, "execute", lambda sql, parameters=None: queries.append(sql)
        or Database.execute(fresh_db, sql, parameters)
    )
    table.changes(50, limit=25)

    change_queries = [query for query in queries if "_sqlite_utils_changes" in query]
    assert change_queries
    assert not any(
        query.lower().lstrip().startswith("select * from")
        for query in queries
    )
    plan = fresh_db.execute(
        "EXPLAIN QUERY PLAN SELECT seq FROM _sqlite_utils_changes "
        'WHERE "table" = ? AND seq > ? ORDER BY seq LIMIT ?',
        ("items", 50, 25),
    ).fetchall()
    assert any("_sqlite_utils_changes_table_seq" in row[-1] for row in plan)
