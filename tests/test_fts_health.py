import pytest
from sqlite_utils.db import FtsRepairError, NotFoundError

RECORDS = [
    {"id": 1, "text": "hello world", "country": "Japan"},
    {"id": 2, "text": "tanuki are running tricksters", "country": "!!!"},
    {"id": 3, "text": 'say "hi" now', "country": None},
]


@pytest.fixture
def fts_table(fresh_db):
    table = fresh_db["searchable"]
    table.insert_all(RECORDS, pk="id")
    table.enable_fts(["text", "country"], create_triggers=True)
    return table


def match_rowids(db, query):
    return [
        row[0]
        for row in db.execute(
            "SELECT rowid FROM searchable_fts WHERE searchable_fts MATCH ?",
            [query],
        ).fetchall()
    ]


def test_check_fts_health_healthy(fts_table):
    report = fts_table.check_fts_health()
    assert report.ok
    assert report.table == "searchable"
    assert report.fts_table == "searchable_fts"
    assert report.columns == ["text", "country"]
    assert not report.index_mismatch
    assert report.missing_triggers == []
    assert report.mismatched_triggers == []
    assert report.unsupported is None


def test_check_fts_health_no_fts(fresh_db):
    fresh_db["plain"].insert({"id": 1}, pk="id")
    with pytest.raises(NotFoundError):
        fresh_db["plain"].check_fts_health()


def test_check_and_repair_desynced_index(fts_table, fresh_db):
    # Manually push the index out of sync, leaving the triggers intact
    fresh_db.execute("UPDATE searchable_fts SET text = 'zzz' WHERE rowid = 1")
    fresh_db.execute(
        "INSERT INTO searchable_fts (rowid, text, country) VALUES (99, 'ghost', 'x')"
    )
    fresh_db.execute(
        "INSERT INTO searchable_fts (searchable_fts, rowid, text, country) "
        "VALUES ('delete', 3, 'say \"hi\" now', NULL)"
    )
    report = fts_table.check_fts_health()
    assert not report.ok
    assert report.index_mismatch
    assert report.missing_triggers == []
    assert report.mismatched_triggers == []
    # Index is currently lying about the content
    assert match_rowids(fresh_db, "hello") == []
    assert match_rowids(fresh_db, "ghost") == [99]
    repaired = fts_table.repair_fts()
    assert repaired.ok
    assert match_rowids(fresh_db, "hello") == [1]
    assert match_rowids(fresh_db, "ghost") == []
    assert match_rowids(fresh_db, "say hi") == [3]
    # Triggers still work after the repair
    fts_table.update(1, {"text": "fresh text"})
    assert match_rowids(fresh_db, "fresh") == [1]
    assert match_rowids(fresh_db, "hello") == []


def test_missing_update_and_delete_triggers(fts_table, fresh_db):
    fresh_db.execute("DROP TRIGGER searchable_au")
    fresh_db.execute("DROP TRIGGER searchable_ad")
    fts_table.update(1, {"text": "updated silently"})
    fresh_db.execute("DELETE FROM searchable WHERE id = 2")
    report = fts_table.check_fts_health()
    assert not report.ok
    assert report.index_mismatch
    assert sorted(report.missing_triggers) == ["searchable_ad", "searchable_au"]
    assert report.mismatched_triggers == []
    repaired = fts_table.repair_fts()
    assert repaired.ok
    assert match_rowids(fresh_db, "updated") == [1]
    assert match_rowids(fresh_db, "tanuki") == []
    # Updating data again is picked up by the restored triggers
    fts_table.update(1, {"text": "updated again"})
    assert match_rowids(fresh_db, "again") == [1]
    fts_table.delete(1)
    assert match_rowids(fresh_db, "again") == []


def test_corrupted_update_trigger(fts_table, fresh_db):
    # Same name, wrong definition - a name-only check would miss this
    fresh_db.execute("DROP TRIGGER searchable_au")
    fresh_db.execute(
        "CREATE TRIGGER searchable_au AFTER UPDATE ON searchable BEGIN "
        "INSERT INTO searchable_fts (rowid, text) VALUES (new.rowid, 'wrong'); "
        "END"
    )
    fts_table.update(1, {"text": "brand new text"})
    report = fts_table.check_fts_health()
    assert not report.ok
    assert report.mismatched_triggers == ["searchable_au"]
    assert report.missing_triggers == []
    assert report.index_mismatch
    repaired = fts_table.repair_fts()
    assert repaired.ok
    assert match_rowids(fresh_db, "brand") == [1]
    assert match_rowids(fresh_db, "wrong") == []
    fts_table.update(1, {"text": "changed once more"})
    assert match_rowids(fresh_db, "changed") == [1]


def test_corrupted_delete_trigger(fts_table, fresh_db):
    fresh_db.execute("DROP TRIGGER searchable_ad")
    fresh_db.execute(
        "CREATE TRIGGER searchable_ad AFTER DELETE ON searchable BEGIN "
        "INSERT INTO searchable_fts (rowid, text, country) "
        "VALUES (old.rowid, 'deleted junk', 'junk'); "
        "END"
    )
    fresh_db.execute("DELETE FROM searchable WHERE id = 2")
    report = fts_table.check_fts_health()
    assert not report.ok
    assert report.mismatched_triggers == ["searchable_ad"]
    assert report.index_mismatch
    repaired = fts_table.repair_fts()
    assert repaired.ok
    assert match_rowids(fresh_db, "tanuki") == []
    assert match_rowids(fresh_db, "junk") == []
    fts_table.delete(3)
    assert match_rowids(fresh_db, "say") == []


def test_repair_fts_second_run_is_noop(fts_table, fresh_db):
    fresh_db.execute("DROP TRIGGER searchable_ai")
    fresh_db.execute("INSERT INTO searchable VALUES (4, 'unindexed row', 'Nowhere')")
    assert not fts_table.check_fts_health().ok
    fts_table.repair_fts()
    snapshot = fresh_db.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2"
    ).fetchall()
    second = fts_table.repair_fts()
    assert second.ok
    assert (
        fresh_db.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2"
        ).fetchall()
        == snapshot
    )
    assert match_rowids(fresh_db, "unindexed") == [4]


def test_repair_fts_preserves_business_triggers(fts_table, fresh_db):
    fresh_db.execute("CREATE TABLE audit_log (message TEXT)")
    fresh_db.execute(
        "CREATE TRIGGER audit_insert AFTER INSERT ON searchable BEGIN "
        "INSERT INTO audit_log VALUES ('row inserted'); "
        "END"
    )
    fresh_db.execute("DROP TRIGGER searchable_au")
    fts_table.repair_fts()
    assert "audit_insert" in fts_table.triggers_dict
    fts_table.insert({"id": 10, "text": "trigger check", "country": "Here"})
    assert fresh_db.execute("SELECT message FROM audit_log").fetchall() == [
        ("row inserted",)
    ]
    assert match_rowids(fresh_db, "trigger") == [10]


def test_repair_fts_refuses_unrecognizable_trigger(fts_table, fresh_db):
    fresh_db.execute("CREATE TABLE audit_log (message TEXT)")
    fresh_db.execute("DROP TRIGGER searchable_ad")
    fresh_db.execute(
        "CREATE TRIGGER searchable_ad AFTER DELETE ON searchable BEGIN "
        "INSERT INTO audit_log VALUES ('deleted'); "
        "END"
    )
    report = fts_table.check_fts_health()
    assert report.mismatched_triggers == ["searchable_ad"]
    with pytest.raises(FtsRepairError):
        fts_table.repair_fts()
    # The custom trigger and the index are both untouched
    assert "searchable_ad" in fts_table.triggers_dict
    assert "audit_log" in fts_table.triggers_dict["searchable_ad"]
    assert match_rowids(fresh_db, "hello") == [1]


def test_repair_fts_rolls_back_on_failure(fts_table, fresh_db, monkeypatch):
    fresh_db.execute("DROP TRIGGER searchable_ad")
    fresh_db.execute("DELETE FROM searchable WHERE id = 2")
    assert match_rowids(fresh_db, "hello") == [1]
    rows_before = fresh_db.execute("SELECT * FROM searchable ORDER BY id").fetchall()

    from sqlite_utils.db import Database

    original_execute = Database.execute

    def failing_execute(self, sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("DROP TRIGGER"):
            raise RuntimeError("simulated failure mid-repair")
        return original_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(Database, "execute", failing_execute)
    with pytest.raises(RuntimeError, match="simulated failure"):
        fts_table.repair_fts()
    monkeypatch.undo()
    # Main table and the original, still usable index are preserved
    assert (
        fresh_db.execute("SELECT * FROM searchable ORDER BY id").fetchall()
        == rows_before
    )
    assert match_rowids(fresh_db, "hello") == [1]
    assert match_rowids(fresh_db, "tanuki") == [2]
    assert "searchable_ad" not in fts_table.triggers_dict
    assert not fresh_db.conn.in_transaction


def test_check_fts_health_fts4_unsupported(fresh_db):
    table = fresh_db["searchable"]
    table.insert({"id": 1, "text": "hello"}, pk="id")
    table.enable_fts(["text"], fts_version="FTS4")
    report = table.check_fts_health()
    assert not report.ok
    assert "FTS5" in report.unsupported
    with pytest.raises(FtsRepairError):
        table.repair_fts()


def test_check_fts_health_standalone_fts5_unsupported(fresh_db):
    fresh_db.execute("CREATE VIRTUAL TABLE standalone USING FTS5 (text)")
    report = fresh_db["standalone"].check_fts_health()
    assert not report.ok
    assert "external content" in report.unsupported
    with pytest.raises(FtsRepairError):
        fresh_db["standalone"].repair_fts()


def test_check_fts_health_without_rowid_unsupported(fresh_db):
    fresh_db.execute("CREATE TABLE wr (id TEXT PRIMARY KEY, text TEXT) WITHOUT ROWID")
    fresh_db.execute('CREATE VIRTUAL TABLE wr_fts USING FTS5 (text, content="wr")')
    report = fresh_db["wr"].check_fts_health()
    assert not report.ok
    assert "WITHOUT ROWID" in report.unsupported


def test_check_fts_health_with_porter_tokenizer(fresh_db):
    table = fresh_db["searchable"]
    table.insert_all(RECORDS, pk="id")
    table.enable_fts(["text"], create_triggers=True, tokenize="porter")
    assert table.check_fts_health().ok
    fresh_db.execute("UPDATE searchable_fts SET text = 'zzz' WHERE rowid = 1")
    report = table.check_fts_health()
    assert report.index_mismatch
    assert table.repair_fts().ok
    assert match_rowids(fresh_db, "hello") == [1]
