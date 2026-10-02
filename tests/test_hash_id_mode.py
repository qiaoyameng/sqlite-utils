"""
Tests for the versioned hash_id mode (hash_id_mode="v1").

The v1 mode hashes records with a deterministic, type-tagged encoding that
does not depend on repr(), JSON serialization, hash randomization or the
current locale. The legacy hash_record() behavior is unchanged.
"""

import copy
import os
import subprocess
import sys
from decimal import Decimal

import pytest

from sqlite_utils.utils import hash_record, hash_record_v1

# Fixed test vectors - these digests must never change, they are the
# long-term content identity contract of the v1 mode.
VECTORS = {
    "simple": (
        ({"name": "Cleo", "twitter": "CleoPaws"}, None),
        "fd3bedc6714fbcf9ff794cc48b6810c186df6045e4f20081790e2751331ae063",
    ),
    "subset_keys": (
        ({"name": "Cleo", "twitter": "CleoPaws", "age": 7}, ("name", "twitter")),
        "fd3bedc6714fbcf9ff794cc48b6810c186df6045e4f20081790e2751331ae063",
    ),
    "all_types": (
        (
            {
                "int": 42,
                "float": 1.5,
                "neg_zero": -0.0,
                "true": True,
                "false": False,
                "str": "héllo",
                "bytes": b"\x00\xff",
                "list": [1, "two", None],
                "dict": {"b": 2, "a": 1},
                "none": None,
                "decimal": Decimal("3.14"),
                "nan": float("nan"),
                "inf": float("inf"),
                "neg_inf": float("-inf"),
            },
            None,
        ),
        "6b402a6b455f0771bd294bb2b7349d064d31cff2d08f2eabcc0485709740a07d",
    ),
    "missing_field": (
        ({"a": 1}, ("a", "b")),
        "923f8123e9e1cbb5a036853d73659abb7c221934594cee0492ea11f7716c0579",
    ),
    "empty": (
        ({}, None),
        "4d30bab6db607226b4ed3a4d914e77b2e4ea0d61d2fab17194df857c13214d4c",
    ),
}


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_fixed_vectors(name):
    (record, keys), expected = VECTORS[name]
    assert hash_record_v1(record, keys) == expected


@pytest.mark.parametrize("seed", ["0", "1", "42"])
def test_vectors_stable_across_hash_seeds(seed):
    # Recompute every vector in a subprocess with a different PYTHONHASHSEED -
    # the digests must be identical, proving no dependence on hash()
    code = (
        "from decimal import Decimal\n"
        "from sqlite_utils.utils import hash_record_v1\n"
        "vectors = [\n"
        "    (({'name': 'Cleo', 'twitter': 'CleoPaws'}, None),"
        " 'fd3bedc6714fbcf9ff794cc48b6810c186df6045e4f20081790e2751331ae063'),\n"
        "    (({'name': 'Cleo', 'twitter': 'CleoPaws', 'age': 7}, ('name', 'twitter')),"
        " 'fd3bedc6714fbcf9ff794cc48b6810c186df6045e4f20081790e2751331ae063'),\n"
        "    (({'int': 42, 'float': 1.5, 'neg_zero': -0.0, 'true': True, 'false': False,"
        " 'str': 'héllo', 'bytes': b'\\x00\\xff', 'list': [1, 'two', None],"
        " 'dict': {'b': 2, 'a': 1}, 'none': None, 'decimal': Decimal('3.14'),"
        " 'nan': float('nan'), 'inf': float('inf'), 'neg_inf': float('-inf')}, None),"
        " '6b402a6b455f0771bd294bb2b7349d064d31cff2d08f2eabcc0485709740a07d'),\n"
        "    (({'a': 1}, ('a', 'b')),"
        " '923f8123e9e1cbb5a036853d73659abb7c221934594cee0492ea11f7716c0579'),\n"
        "    (({}, None),"
        " '4d30bab6db607226b4ed3a4d914e77b2e4ea0d61d2fab17194df857c13214d4c'),\n"
        "]\n"
        "for (record, keys), expected in vectors:\n"
        "    actual = hash_record_v1(record, keys)\n"
        "    assert actual == expected, (record, actual)\n"
    )
    env = dict(os.environ, PYTHONHASHSEED=seed)
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_nested_dict_key_order_does_not_change_hash():
    first = {"outer": {"x": 1, "y": {"b": 2, "a": 1}}, "list": [1, 2]}
    reordered = {"list": [1, 2], "outer": {"y": {"a": 1, "b": 2}, "x": 1}}
    assert hash_record_v1(first) == hash_record_v1(reordered)


def test_list_order_changes_hash():
    assert hash_record_v1({"a": [1, 2, 3]}) != hash_record_v1({"a": [3, 2, 1]})


def test_missing_field_differs_from_none():
    assert hash_record_v1({"a": 1}, keys=("a", "b")) != hash_record_v1(
        {"a": 1, "b": None}, keys=("a", "b")
    )


def test_type_boundaries():
    # bool is not int, int is not float, str is not bytes
    assert hash_record_v1({"v": True}) != hash_record_v1({"v": 1})
    assert hash_record_v1({"v": 1}) != hash_record_v1({"v": 1.0})
    assert hash_record_v1({"v": "1"}) != hash_record_v1({"v": 1})
    assert hash_record_v1({"v": b"abc"}) != hash_record_v1({"v": "abc"})
    assert hash_record_v1({"v": Decimal("1.5")}) != hash_record_v1({"v": 1.5})


def test_float_and_decimal_rules():
    # -0.0 normalizes to 0.0
    assert hash_record_v1({"v": -0.0}) == hash_record_v1({"v": 0.0})
    # NaN hashes deterministically (and does not depend on the NaN payload)
    assert hash_record_v1({"v": float("nan")}) == hash_record_v1({"v": float("nan")})
    assert hash_record_v1({"v": float("nan")}) != hash_record_v1({"v": float("inf")})
    assert hash_record_v1({"v": float("inf")}) != hash_record_v1({"v": float("-inf")})
    # Decimal trailing zeros and negative zero normalize
    assert hash_record_v1({"v": Decimal("1.0")}) == hash_record_v1(
        {"v": Decimal("1.00")}
    )
    assert hash_record_v1({"v": Decimal("-0")}) == hash_record_v1({"v": Decimal("0")})
    assert hash_record_v1({"v": Decimal("NaN")}) == hash_record_v1(
        {"v": Decimal("NaN")}
    )


def test_field_identity_separates_namespaces():
    # Same values under different field sets must not collide
    assert hash_record_v1({"a": 1}) != hash_record_v1({"b": 1})
    assert hash_record_v1({"a": 1}, keys=("a",)) != hash_record_v1(
        {"a": 1}, keys=("a", "b")
    )


def test_input_record_is_not_modified():
    record = {
        "name": "Cleo",
        "tags": ["a", "b"],
        "meta": {"x": 1, "y": None},
        "blob": b"hi",
    }
    snapshot = copy.deepcopy(record)
    hash_record_v1(record)
    hash_record_v1(record, keys=("name", "tags", "missing"))
    assert record == snapshot


@pytest.mark.parametrize("bad_value", [object(), {1, 2}, Exception("nope")])
def test_unhashable_value_raises(bad_value):
    with pytest.raises(TypeError):
        hash_record_v1({"value": bad_value})


def test_non_string_dict_key_raises():
    with pytest.raises(TypeError):
        hash_record_v1({"nested": {1: "one"}})


def test_insert_v1_uses_versioned_hash(fresh_db):
    record = {"name": "Cleo", "twitter": "cleopaws"}
    table = fresh_db.table("dogs").insert(record, hash_id="id", hash_id_mode="v1")
    expected = hash_record_v1(record)
    assert table.last_pk == expected
    assert fresh_db["dogs"].get(expected)["name"] == "Cleo"


def test_insert_and_upsert_share_the_same_rule(fresh_db):
    record = {"name": "Cleo", "twitter": "cleopaws"}
    fresh_db.table("dogs").insert(record, hash_id="id", hash_id_mode="v1")
    fresh_db.table("dogs").upsert(
        {"name": "Pancakes", "twitter": "pancakes"}, hash_id="id", hash_id_mode="v1"
    )
    pks = [row["id"] for row in fresh_db["dogs"].rows]
    assert pks == [
        hash_record_v1(record),
        hash_record_v1({"name": "Pancakes", "twitter": "pancakes"}),
    ]


def test_insert_all_and_upsert_all_v1(fresh_db):
    records = [
        {"name": "Cleo", "age": 5},
        {"name": "Pancakes", "age": 4},
    ]
    fresh_db.table("dogs").insert_all(records, hash_id="id", hash_id_mode="v1")
    assert [row["id"] for row in fresh_db["dogs"].rows] == [
        hash_record_v1(record) for record in records
    ]
    # Upserting identical content is a no-op, not a duplicate
    fresh_db.table("dogs").upsert_all(records, hash_id="id", hash_id_mode="v1")
    assert fresh_db["dogs"].count == 2


def test_insert_does_not_mutate_record(fresh_db):
    record = {"name": "Cleo", "tags": ["a"], "meta": {"x": 1}}
    snapshot = copy.deepcopy(record)
    fresh_db.table("dogs").insert(record, hash_id="id", hash_id_mode="v1")
    assert record == snapshot


def test_invalid_value_fails_before_any_write(fresh_db):
    with pytest.raises(TypeError):
        fresh_db.table("dogs").insert_all(
            [{"name": "Cleo"}, {"name": "Pancakes", "bad": object()}],
            hash_id="id",
            hash_id_mode="v1",
        )
    # Nothing was written - the table was not even created
    assert not fresh_db["dogs"].exists()


def test_invalid_mode_rejected_before_any_write(fresh_db):
    with pytest.raises(ValueError, match="hash_id_mode"):
        fresh_db.table("dogs").insert({"name": "Cleo"}, hash_id="id", hash_id_mode="v2")
    assert not fresh_db["dogs"].exists()


def test_legacy_mode_is_unchanged(fresh_db):
    record = {"name": "Cleo", "twitter": "cleopaws"}
    table = fresh_db.table("dogs").insert(record, hash_id="id")
    assert table.last_pk == hash_record(record)
    # Explicit "legacy" is the same as the default
    table2 = fresh_db.table("cats").insert(record, hash_id="id", hash_id_mode="legacy")
    assert table2.last_pk == hash_record(record)


def test_legacy_primary_keys_are_not_reinterpreted(fresh_db):
    record = {"name": "Cleo", "twitter": "cleopaws"}
    legacy_pk = hash_record(record)
    fresh_db.table("dogs").insert(record, hash_id="id")
    # Upserting the same content in v1 mode must not match the legacy row -
    # the two modes live in separate namespaces
    fresh_db.table("dogs").upsert(record, hash_id="id", hash_id_mode="v1")
    assert fresh_db["dogs"].count == 2
    # The legacy row still has its original primary key
    assert fresh_db["dogs"].get(legacy_pk)["name"] == "Cleo"
    assert legacy_pk != hash_record_v1(record)


def test_hash_id_mode_table_default(fresh_db):
    dogs = fresh_db.table("dogs", hash_id="id", hash_id_mode="v1")
    record = {"name": "Cleo"}
    dogs.insert(record)
    assert dogs.last_pk == hash_record_v1(record)
    dogs.upsert({"name": "Pancakes"})
    assert dogs.count == 2
