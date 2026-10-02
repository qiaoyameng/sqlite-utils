import math
import os
import subprocess
import sys
from decimal import Decimal

import pytest

from sqlite_utils.utils import HASH_ID_VERSION_1, HashIdError, hash_record_v1

VECTOR = "b6c6f0abb4ac5587c389336184cd332206b2038d0ff227e68c6582837eac86cf"
NESTED_VECTOR = "f432ea5e75b805a3adf959e0db696dec1ffa5840d80eb15645cd9acebbe3afc4"
DECIMAL_VECTOR = "c32a0f6759a8a8e1b24c6ddc0dfc27f92cf5300a1875b8333b60ba15cf832a1c"


def test_hash_record_v1_fixed_vector():
    assert (
        hash_record_v1(
            {"name": "Cleo", "twitter": "cleopaws"},
            ("name", "twitter"),
        )
        == VECTOR
    )


def test_hash_record_v1_fixed_nested_vector():
    assert NESTED_VECTOR == hash_record_v1(
        {
            "profile": {"active": True, "name": "Cleo"},
            "tags": ["a", "b"],
        },
        ("tags", "profile"),
    )


def test_hash_record_v1_fixed_decimal_vector():
    assert DECIMAL_VECTOR == hash_record_v1({"d": Decimal("1.20")}, ("d",))


def test_hash_record_v1_vector_is_stable_across_python_hash_seeds():
    code = """
from sqlite_utils.utils import hash_record_v1
from decimal import Decimal
values = (
    hash_record_v1({"name": "Cleo", "twitter": "cleopaws"}, ("name", "twitter")),
    hash_record_v1(
        {"profile": {"active": True, "name": "Cleo"}, "tags": ["a", "b"]},
        ("tags", "profile"),
    ),
    hash_record_v1({"d": Decimal("1.20")}, ("d",)),
)
print("\\n".join(values))
"""
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
    values = {
        subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={**env, "PYTHONHASHSEED": str(seed)},
        ).stdout.strip()
        for seed in ("0", "1", "random")
    }
    expected = f"{VECTOR}\n{NESTED_VECTOR}\n{DECIMAL_VECTOR}"
    assert values == {expected}


def test_hash_record_v1_mapping_order_is_insignificant_but_list_order_matters():
    record = {
        "profile": {"name": "Cleo", "active": True},
        "tags": ["a", "b"],
    }
    reordered = {
        "profile": {"active": True, "name": "Cleo"},
        "tags": ["a", "b"],
    }
    assert hash_record_v1(record, ("profile", "tags")) == hash_record_v1(
        reordered, ("tags", "profile")
    )
    assert hash_record_v1(record, ("profile", "tags")) != hash_record_v1(
        {"profile": reordered["profile"], "tags": ["b", "a"]}, ("profile", "tags")
    )


def test_hash_record_v1_missing_is_distinct_from_null():
    with_null = hash_record_v1({"name": None}, ("name", "age"))
    missing = hash_record_v1({}, ("name", "age"))
    both_null = hash_record_v1({"name": None, "age": None}, ("name", "age"))
    assert len({with_null, missing, both_null}) == 3


@pytest.mark.parametrize(
    "left,right",
    (
        (True, 1),
        (1, "1"),
        ("1", b"1"),
        (1, 1.0),
        (1.0, Decimal("1.0")),
    ),
)
def test_hash_record_v1_preserves_type_boundaries(left, right):
    assert hash_record_v1({"value": left}, ("value",)) != hash_record_v1(
        {"value": right}, ("value",)
    )


def test_hash_record_v1_handles_float_zero_decimal_and_rejects_non_finite_values():
    assert hash_record_v1({"value": 0.0}, ("value",)) == hash_record_v1(
        {"value": -0.0}, ("value",)
    )
    assert hash_record_v1({"value": Decimal("1.20")}, ("value",)) == hash_record_v1(
        {"value": Decimal("1.20")}, ("value",)
    )
    for value in (math.nan, math.inf, -math.inf, Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(HashIdError):
            hash_record_v1({"value": value}, ("value",))


@pytest.mark.parametrize(
    "keys",
    (
        (),
        ("name", "name"),
        (1,),
    ),
)
def test_hash_record_v1_rejects_invalid_field_lists(keys):
    with pytest.raises(HashIdError):
        hash_record_v1({"name": "Cleo"}, keys)


def test_hash_record_v1_rejects_unsupported_mapping_keys_and_cyclic_values():
    with pytest.raises(HashIdError):
        hash_record_v1({"value": {1: "x"}}, ("value",))
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(HashIdError):
        hash_record_v1(cyclic, ("self",))


def test_hash_record_v1_includes_version_fields_and_field_order_namespace():
    record = {"name": "Cleo", "twitter": "cleopaws"}
    assert hash_record_v1(record, ("name", "twitter")) == hash_record_v1(
        record, ("twitter", "name")
    )
    assert hash_record_v1(record, ("name", "twitter")) != hash_record_v1(
        record, ("name",)
    )
    assert hash_record_v1({"a": 1, "name": 1}, ("name",)) == hash_record_v1(
        {"b": 1, "name": 1}, ("name",)
    )
    assert hash_record_v1({"a": 1, "b": 1}, ("a", "b")) != hash_record_v1(
        {"a": 1, "b": 1}, ("b",)
    )


@pytest.mark.parametrize("method", ("insert", "upsert", "insert_all", "upsert_all"))
def test_insert_and_upsert_use_hash_record_v1(fresh_db, method):
    table = fresh_db.table("people")
    kwargs = {
        "hash_id": "id",
        "hash_id_columns": ("name",),
        "hash_id_version": HASH_ID_VERSION_1,
        "alter": True,
    }
    payload = [{"name": "Cleo", "extra": "one"}]
    first = getattr(table, method)(
        payload if "all" in method else payload[0], **kwargs
    )
    second = table.upsert_all(
        [{"name": "Cleo", "extra": "two"}],
        hash_id="id",
        hash_id_columns=("name",),
        hash_id_version=HASH_ID_VERSION_1,
        alter=True,
    )
    expected = hash_record_v1({"name": "Cleo"}, ("name",))
    assert first.last_pk == expected
    assert second.last_pk == expected
    assert table.count == 1
    assert table.get(expected)["extra"] == "two"


def test_table_factory_uses_hash_record_v1(fresh_db):
    table = fresh_db.table(
        "people",
        hash_id="id",
        hash_id_columns=("name",),
        hash_id_version=HASH_ID_VERSION_1,
    )
    assert (
        table.insert({"name": "Cleo"}).last_pk
        == hash_record_v1({"name": "Cleo"}, ("name",))
    )


def test_hash_id_v1_rejects_invalid_values_before_any_write(fresh_db):
    fresh_db["people"].insert({"id": "old"})
    records = [
        {"name": "Cleo"},
        {"name": math.nan},
    ]
    with pytest.raises(HashIdError):
        fresh_db.table("people").insert_all(
            records,
            hash_id="id",
            hash_id_columns=("name",),
            hash_id_version=HASH_ID_VERSION_1,
            alter=True,
        )
    assert list(fresh_db["people"].rows) == [{"id": "old"}]


def test_hash_id_v1_rejects_invalid_values_before_truncate(fresh_db):
    fresh_db["people"].insert({"id": "old"})
    with pytest.raises(HashIdError):
        fresh_db.table("people").insert_all(
            [{"name": "Cleo"}, {"name": math.inf}],
            hash_id="id",
            hash_id_columns=("name",),
            hash_id_version=HASH_ID_VERSION_1,
            truncate=True,
            alter=True,
        )
    assert list(fresh_db["people"].rows) == [{"id": "old"}]


def test_hash_id_v1_does_not_mutate_input_dicts(fresh_db):
    record = {
        "name": "Cleo",
        "profile": {"active": True},
        "tags": ["a", "b"],
    }
    snapshot = dict(record)
    fresh_db.table("people").insert(
        record,
        hash_id="id",
        hash_id_columns=("name", "profile", "tags"),
        hash_id_version=HASH_ID_VERSION_1,
    )
    assert record == snapshot
    assert record["profile"] is snapshot["profile"]
    assert record["tags"] is snapshot["tags"]


def test_old_hash_id_primary_key_is_not_reinterpreted_as_v1(fresh_db):
    table = fresh_db.table("people")
    old_id = table.insert({"name": "Cleo"}, hash_id="id").last_pk
    table.upsert(
        {"name": "Cleo"},
        hash_id="id",
        hash_id_columns=("name",),
        hash_id_version=HASH_ID_VERSION_1,
    )
    assert table.count == 2
    assert len(old_id) == 40
    assert all(len(row["id"]) == 64 for row in table.rows if row["id"] != old_id)
