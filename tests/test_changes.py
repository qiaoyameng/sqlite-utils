import pytest

from sqlite_utils import OP_DELETE, OP_INSERT, OP_UPDATE, Database
from sqlite_utils.db import (
    ChangeConsumerNotFound,
    ChangeLogGap,
)


def change_ids(batch):
    return [change["id"] for change in batch.changes]


def test_change_log_records_insert_update_delete(fresh_db):
    fresh_db["items"].insert({"id": 1, "name": "before"}, pk="id")
    fresh_db["items"].enable_changes()

    fresh_db["items"].insert({"id": 2, "name": "new"})
    fresh_db["items"].update(2, {"name": "updated"})
    fresh_db["items"].delete(2)

    changes = fresh_db.read_changes(batch_size=10).changes
    assert [change["op"] for change in changes] == [
        OP_INSERT,
        OP_UPDATE,
        OP_DELETE,
    ]
    assert changes[0]["new_pk"] == 2
    assert changes[0]["new"] == {"id": 2, "name": "new"}
    assert changes[1]["old_pk"] == 2
    assert changes[1]["new_pk"] == 2
    assert changes[1]["changed_columns"] == ["name"]
    assert changes[2]["old_pk"] == 2
    assert changes[2]["old"] == {"id": 2, "name": "updated"}
    assert changes[0].operation == OP_INSERT
    assert changes[0].new_primary_key == 2


def test_change_log_distinguishes_implicit_rowid_from_rowid_column(fresh_db):
    fresh_db.execute("CREATE TABLE items (rowid TEXT, name TEXT)")
    fresh_db["items"].enable_changes()

    fresh_db.execute("INSERT INTO items (rowid, name) VALUES ('key', 'x')")
    fresh_db.execute("UPDATE items SET name = 'y' WHERE rowid = 'key'")

    inserted, updated = fresh_db.read_changes(batch_size=10).changes
    assert inserted.new_pk == 1
    assert inserted.new == {"rowid": "key", "name": "x"}
    assert updated.old_pk == 1
    assert updated.new_pk == 1
    assert updated.changed_columns == ["name"]


@pytest.mark.parametrize(
    "schema,insert_sql,update_sql,delete_sql",
    (
        (
            "CREATE TABLE compound (a INTEGER, b INTEGER, name TEXT, "
            "PRIMARY KEY (b, a)) WITHOUT ROWID",
            "INSERT INTO compound VALUES (1, 2, 'x')",
            "UPDATE compound SET a = 3, b = 4, name = 'y'",
            "DELETE FROM compound WHERE a = 3 AND b = 4",
        ),
        (
            "CREATE TABLE single (id TEXT PRIMARY KEY, name TEXT) WITHOUT ROWID",
            "INSERT INTO single VALUES ('old', 'x')",
            "UPDATE single SET id = 'new', name = 'y'",
            "DELETE FROM single WHERE id = 'new'",
        ),
    ),
)
def test_change_log_supports_without_rowid_and_primary_key_updates(
    fresh_db, schema, insert_sql, update_sql, delete_sql
):
    table_name = schema.split()[2]
    fresh_db.execute(schema)
    fresh_db[table_name].enable_changes()

    fresh_db.execute(insert_sql)
    fresh_db.execute(update_sql)
    fresh_db.execute(delete_sql)

    inserted, updated, deleted = fresh_db[table_name].read_changes(
        batch_size=10
    ).changes
    if table_name == "compound":
        assert inserted["new_pk"] == (2, 1)
        assert updated["old_pk"] == (2, 1)
        assert updated["new_pk"] == (4, 3)
        assert updated["changed_columns"] == ["a", "b", "name"]
        assert deleted["old_pk"] == (4, 3)
    else:
        assert inserted["new_pk"] == "old"
        assert updated["old_pk"] == "old"
        assert updated["new_pk"] == "new"
        assert deleted["old_pk"] == "new"


def test_change_log_delete_then_reinsert_same_primary_key(fresh_db):
    fresh_db["items"].insert({"id": 1, "name": "first"}, pk="id")
    fresh_db["items"].enable_changes()

    fresh_db["items"].delete(1)
    fresh_db["items"].insert({"id": 1, "name": "second"}, pk="id")

    deleted, inserted = fresh_db.read_changes(batch_size=10).changes
    assert deleted["op"] == OP_DELETE
    assert deleted["old_pk"] == 1
    assert inserted["op"] == OP_INSERT
    assert inserted["new_pk"] == 1
    assert inserted["new"] == {"id": 1, "name": "second"}


def test_change_log_transaction_and_savepoint_rollback(fresh_db):
    fresh_db["items"].insert({"id": 1, "name": "base"}, pk="id")
    fresh_db["items"].enable_changes()

    with pytest.raises(RuntimeError), fresh_db.atomic():
        fresh_db.execute("INSERT INTO items (id, name) VALUES (2, 'rolled back')")
        raise RuntimeError("rollback")

    with fresh_db.atomic():
        fresh_db.execute("INSERT INTO items (id, name) VALUES (2, 'kept')")
        with pytest.raises(RuntimeError), fresh_db.atomic():
            fresh_db.execute("INSERT INTO items (id, name) VALUES (3, 'savepoint')")
            raise RuntimeError("savepoint rollback")
        fresh_db.execute("INSERT INTO items (id, name) VALUES (4, 'also kept')")

    changes = fresh_db.read_changes(batch_size=10).changes
    assert [(change["id"], change["new_pk"]) for change in changes] == [(1, 2), (2, 4)]


def test_change_log_enable_repeat_disable_only_manages_its_triggers(fresh_db):
    fresh_db["items"].insert({"id": 1, "name": "x"}, pk="id")
    fresh_db.execute(
        "CREATE TRIGGER items_user_trigger AFTER UPDATE ON items BEGIN SELECT 1; END"
    )
    fresh_db["items"].enable_counts()

    fresh_db["items"].enable_changes()
    original_sql = fresh_db["items"].triggers_dict["items_user_trigger"]
    fresh_db["items"].enable_changes()
    fresh_db.enable_changes()
    fresh_db["items"].disable_changes()

    assert "items_changes_insert" not in fresh_db["items"].triggers_dict
    assert "items_changes_update" not in fresh_db["items"].triggers_dict
    assert "items_changes_delete" not in fresh_db["items"].triggers_dict
    assert "items_counts_insert" in fresh_db["items"].triggers_dict
    assert "items_counts_delete" in fresh_db["items"].triggers_dict
    assert fresh_db["items"].triggers_dict["items_user_trigger"] == original_sql


def test_change_log_rejects_unmanaged_trigger_name_collision(fresh_db):
    fresh_db["items"].insert({"id": 1, "name": "x"}, pk="id")
    fresh_db.execute(
        "CREATE TRIGGER items_changes_insert AFTER INSERT ON items BEGIN SELECT 1; END"
    )
    with pytest.raises(Exception, match="not managed"):
        fresh_db["items"].enable_changes()


def test_change_log_batches_resume_in_order(fresh_db):
    fresh_db["items"].create({"id": int, "name": str}, pk="id")
    fresh_db["items"].enable_changes()
    fresh_db["items"].insert_all(
        [{"id": number, "name": str(number)} for number in range(1, 7)]
    )

    first = fresh_db.read_changes(batch_size=2)
    assert change_ids(first) == [1, 2]
    assert first.done is False
    second = fresh_db.read_changes(cursor=first.next_cursor, batch_size=2)
    assert change_ids(second) == [3, 4]
    third = fresh_db.read_changes(cursor=second.next_cursor, batch_size=2)
    assert change_ids(third) == [5, 6]
    assert third.done is True
    empty = fresh_db.read_changes(cursor=third.next_cursor, batch_size=2)
    assert empty.changes == []
    assert empty.next_cursor == third.next_cursor
    assert empty.done is True


def test_change_log_consumers_acknowledgment_pruning_and_gap(fresh_db):
    fresh_db["items"].create({"id": int}, pk="id")
    fresh_db["items"].enable_changes()
    fresh_db["items"].insert_all([{"id": number} for number in range(1, 7)])
    fresh_db.create_change_consumer("indexer")

    batch = fresh_db.read_changes(batch_size=3)
    assert change_ids(batch) == [1, 2, 3]
    assert fresh_db.change_consumer_cursor("indexer") == 0
    fresh_db.acknowledge_changes("indexer", batch.next_cursor)
    assert fresh_db.change_consumer_cursor("indexer") == 3
    assert fresh_db.prune_changes(consumer="indexer") == 3
    assert fresh_db.execute("SELECT MIN(id), MAX(id) FROM _changes").fetchone() == (
        4,
        6,
    )

    with pytest.raises(ChangeLogGap, match="behind the retained watermark 3"):
        fresh_db.read_changes(cursor=2)
    resumed = fresh_db.read_changes(cursor=3, batch_size=10)
    assert change_ids(resumed) == [4, 5, 6]

    with pytest.raises(ChangeConsumerNotFound):
        fresh_db.acknowledge_changes("missing", 3)
    with pytest.raises(ChangeConsumerNotFound):
        fresh_db.prune_changes(consumer="missing")


def test_change_log_pruning_preserves_events_for_slower_consumer(fresh_db):
    fresh_db["items"].create({"id": int}, pk="id")
    fresh_db["items"].enable_changes()
    fresh_db["items"].insert_all([{"id": number} for number in range(1, 5)])
    fresh_db.create_change_consumer("fast")
    fresh_db.create_change_consumer("slow")

    fresh_db.acknowledge_changes("fast", 4)
    fresh_db.acknowledge_changes("slow", 2)
    assert fresh_db.prune_changes() == 2
    assert fresh_db.execute("SELECT MIN(id) FROM _changes").fetchone()[0] == 3
    assert fresh_db.prune_changes(cursor=10) == 2
    with pytest.raises(ChangeLogGap):
        fresh_db.read_changes(cursor=3)
    with pytest.raises(ChangeLogGap):
        fresh_db.create_change_consumer("late", cursor=3)


def test_change_log_remains_readable_after_table_is_dropped(fresh_db):
    fresh_db.execute(
        "CREATE TABLE compound (a INTEGER, b INTEGER, PRIMARY KEY (b, a)) WITHOUT ROWID"
    )
    fresh_db["compound"].enable_changes()
    fresh_db.execute("INSERT INTO compound VALUES (1, 2)")
    fresh_db.execute("DROP TABLE compound")

    change = fresh_db.read_changes().changes[0]
    assert change.table == "compound"
    assert change.new_pk == (2, 1)
    assert change.new == {"b": 2, "a": 1}


def test_change_log_visibility_between_two_connections(tmp_path):
    path = tmp_path / "changes.db"
    writer = Database(path)
    reader = Database(path)
    writer["items"].create({"id": int, "name": str}, pk="id")
    writer["items"].enable_changes()

    writer.begin()
    writer.execute("INSERT INTO items (id, name) VALUES (1, 'uncommitted')")
    assert reader.read_changes().changes == []

    writer.execute("INSERT INTO items (id, name) VALUES (2, 'also uncommitted')")
    own_uncommitted = writer.read_changes(batch_size=10)
    assert change_ids(own_uncommitted) == [1, 2]
    assert reader.read_changes().changes == []

    writer.commit()
    committed = reader.read_changes(batch_size=10).changes
    assert [change["new_pk"] for change in committed] == [1, 2]

    writer.begin()
    writer.execute("INSERT INTO items (id, name) VALUES (3, 'will rollback')")
    writer.rollback()
    writer.commit()
    assert change_ids(reader.read_changes(cursor=2)) == []

    first = reader.read_changes(batch_size=1)
    assert change_ids(first) == [1]
    second = reader.read_changes(cursor=first.next_cursor, batch_size=1)
    assert change_ids(second) == [2]


def test_change_log_does_not_require_scanning_business_table(fresh_db):
    fresh_db["items"].create({"id": int}, pk="id")
    fresh_db["items"].enable_changes()
    fresh_db["items"].insert_all([{"id": number} for number in range(1, 4)])
    statements = []

    with fresh_db.tracer(lambda sql, params: statements.append((sql, params))):
        fresh_db.read_changes(batch_size=10)
        fresh_db.create_change_consumer("x")
        fresh_db.acknowledge_changes("x", 3)
        fresh_db.prune_changes(consumer="x")

    assert not any(
        "FROM \"items\"" in sql or "from \"items\"" in sql for sql, _ in statements
    )
