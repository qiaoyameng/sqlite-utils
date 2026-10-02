import pytest

from sqlite_utils.db import (
    FTS_ACTION_REBUILD_INDEX,
    FTS_ACTION_RECREATE_TRIGGERS,
    FTS_ISSUE_CONTENT_MISMATCH,
    FTS_ISSUE_MISSING_TRIGGERS,
    FTS_ISSUE_TRIGGER_MISMATCH,
    FTS_ISSUE_UNSUPPORTED_LAYOUT,
    FTSUnsupportedError,
)
from sqlite_utils.utils import sqlite3


@pytest.fixture
def docs_table(fresh_db):
    table = fresh_db.table("docs")
    table.insert_all(
        [
            {"id": 1, "title": "hello world"},
            {"id": 2, "title": "goodbye world"},
        ],
        pk="id",
    )
    table.enable_fts(["title"], create_triggers=True)
    return table


def test_check_fts_healthy(docs_table):
    report = docs_table.check_fts()
    assert report.healthy
    assert report.issues == []
    assert report.table == "docs"
    assert report.fts_table == "docs_fts"
    assert report.details["columns"] == ["title"]


def test_check_and_repair_fts_desync(docs_table, fresh_db):
    # Simulate a manual import that bypassed the synchronization trigger
    fresh_db.execute("DROP TRIGGER docs_ai")
    docs_table.insert({"id": 3, "title": "sneaky manual import"})
    report = docs_table.check_fts()
    assert not report.healthy
    assert set(report.issues) == {
        FTS_ISSUE_CONTENT_MISMATCH,
        FTS_ISSUE_MISSING_TRIGGERS,
    }
    assert report.details["missing_rowids"] == [3]
    assert report.details["orphaned_rowids"] == []
    assert report.details["missing_triggers"] == ["docs_ai"]
    # The manually imported row is not searchable yet
    assert list(docs_table.search("sneaky")) == []
    result = docs_table.repair_fts()
    assert result.repaired
    assert result.actions == [FTS_ACTION_REBUILD_INDEX, FTS_ACTION_RECREATE_TRIGGERS]
    assert result.report.healthy
    # MATCH now finds the manually imported row
    assert [row["title"] for row in docs_table.search("sneaky")] == [
        "sneaky manual import"
    ]
    # The recreated insert trigger keeps the index in sync again
    docs_table.insert({"id": 4, "title": "another sneaky addition"})
    assert {row["id"] for row in docs_table.search("sneaky")} == {3, 4}


def test_repair_fts_is_idempotent(docs_table):
    first = docs_table.repair_fts()
    assert not first.repaired
    assert first.actions == []
    assert first.report.healthy
    # Corrupt a trigger, repair, then repair again - second is a no-op
    docs_table.db.execute("DROP TRIGGER docs_au")
    assert docs_table.repair_fts().repaired
    second = docs_table.repair_fts()
    assert not second.repaired
    assert second.actions == []
    assert second.report.healthy


@pytest.mark.parametrize("trigger", ["docs_au", "docs_ad"])
def test_check_and_repair_fts_tampered_trigger(docs_table, fresh_db, trigger):
    # Replace a synchronization trigger with a same-named no-op imposter
    timing = "UPDATE" if trigger == "docs_au" else "DELETE"
    fresh_db.execute(f"DROP TRIGGER {trigger}")
    fresh_db.execute(
        f"CREATE TRIGGER {trigger} AFTER {timing} ON docs BEGIN SELECT 1; END"
    )
    report = docs_table.check_fts()
    # Name alone must not be considered healthy - the definition is wrong
    assert report.issues == [FTS_ISSUE_TRIGGER_MISMATCH]
    assert report.details["mismatched_triggers"] == [trigger]
    result = docs_table.repair_fts()
    assert result.repaired
    assert result.actions == [FTS_ACTION_REBUILD_INDEX, FTS_ACTION_RECREATE_TRIGGERS]
    assert result.report.healthy
    assert docs_table.check_fts().healthy
    if trigger == "docs_au":
        # Updating data again flows into the index
        docs_table.update(1, {"title": "hello brave new world"})
        assert [row["id"] for row in docs_table.search("brave")] == [1]
        assert list(docs_table.search("goodbye")) != []
    else:
        # Deleting data again removes it from the index
        docs_table.delete(2)
        assert list(docs_table.search("goodbye")) == []
        assert [row["id"] for row in docs_table.search("hello")] == [1]


def test_repair_fts_stale_content_from_tampered_trigger(docs_table, fresh_db):
    # An update applied while the trigger was tampered with is healed by repair
    fresh_db.execute("DROP TRIGGER docs_au")
    fresh_db.execute("CREATE TRIGGER docs_au AFTER UPDATE ON docs BEGIN SELECT 1; END")
    docs_table.update(1, {"title": "completely rewritten"})
    assert list(docs_table.search("rewritten")) == []
    report = docs_table.check_fts()
    assert FTS_ISSUE_TRIGGER_MISMATCH in report.issues
    docs_table.repair_fts()
    assert [row["id"] for row in docs_table.search("rewritten")] == [1]


def test_repair_fts_preserves_other_triggers(docs_table, fresh_db):
    fresh_db.execute("CREATE TABLE audit_log (message TEXT)")
    fresh_db.execute(
        "CREATE TRIGGER docs_audit AFTER INSERT ON docs BEGIN "
        "INSERT INTO audit_log VALUES (new.title); END"
    )
    fresh_db.execute("DROP TRIGGER docs_ad")
    docs_table.repair_fts()
    assert docs_table.check_fts().healthy
    # The business trigger survived the repair and still works
    assert "docs_audit" in docs_table.triggers_dict
    docs_table.insert({"id": 5, "title": "audited row"})
    assert list(fresh_db["audit_log"].rows) == [{"message": "audited row"}]


def test_repair_fts_failure_rolls_back(docs_table, fresh_db, monkeypatch):
    # Corrupt the delete trigger so a repair has work to do
    fresh_db.execute("DROP TRIGGER docs_ad")
    assert list(docs_table.search("hello")) != []
    original_execute = fresh_db.execute

    def failing_execute(sql, *args, **kwargs):
        if "CREATE TRIGGER" in str(sql) and "docs_au" in str(sql):
            raise sqlite3.OperationalError("simulated failure mid-repair")
        return original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(fresh_db, "execute", failing_execute)
    with pytest.raises(sqlite3.OperationalError, match="simulated failure"):
        docs_table.repair_fts()
    monkeypatch.undo()
    # The main table is intact
    assert list(fresh_db["docs"].rows) == [
        {"id": 1, "title": "hello world"},
        {"id": 2, "title": "goodbye world"},
    ]
    # The original, still usable index was preserved by the rollback
    assert [row["id"] for row in docs_table.search("hello")] == [1]
    assert [row["id"] for row in docs_table.search("goodbye")] == [2]
    # The healthy triggers were not dropped, the damaged one is still absent
    assert set(docs_table.triggers_dict) == {"docs_ai", "docs_au"}
    # A repair can be retried successfully afterwards
    assert docs_table.repair_fts().repaired
    assert docs_table.check_fts().healthy


def test_check_fts_no_fts_table(fresh_db):
    fresh_db.table("plain").insert({"id": 1}, pk="id")
    report = fresh_db["plain"].check_fts()
    assert not report.healthy
    assert report.issues == [FTS_ISSUE_UNSUPPORTED_LAYOUT]
    assert report.fts_table is None
    with pytest.raises(FTSUnsupportedError):
        fresh_db["plain"].repair_fts()


def test_check_fts_fts4_unsupported(fresh_db):
    table = fresh_db.table("legacy")
    table.insert({"id": 1, "title": "hello"}, pk="id")
    table.enable_fts(["title"], fts_version="FTS4", create_triggers=True)
    report = table.check_fts()
    assert report.issues == [FTS_ISSUE_UNSUPPORTED_LAYOUT]
    with pytest.raises(FTSUnsupportedError, match="FTS5"):
        table.repair_fts()


def test_check_fts_contentless_unsupported(fresh_db):
    fresh_db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT)")
    fresh_db.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(title, content='')")
    report = fresh_db["docs"].check_fts()
    assert report.issues == [FTS_ISSUE_UNSUPPORTED_LAYOUT]
    with pytest.raises(FTSUnsupportedError):
        fresh_db["docs"].repair_fts()


def test_check_fts_columnsize_zero_unsupported(fresh_db):
    fresh_db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT)")
    fresh_db.execute(
        'CREATE VIRTUAL TABLE docs_fts USING fts5(title, content="docs", columnsize=0)'
    )
    report = fresh_db["docs"].check_fts()
    assert report.issues == [FTS_ISSUE_UNSUPPORTED_LAYOUT]
    assert "columnsize" in report.details["reason"]
    with pytest.raises(FTSUnsupportedError):
        fresh_db["docs"].repair_fts()
