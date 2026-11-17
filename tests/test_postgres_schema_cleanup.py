from contextlib import contextmanager

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from tests.postgres_schema_support import (
    new_schema_name,
    temporary_owned_schema,
    url_for_schema,
    validate_owned_schema_name,
)


class FakeConnection:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.engine.statements.append(sql)
        if sql.startswith("CREATE SCHEMA "):
            schema = sql.removeprefix("CREATE SCHEMA ").strip('"')
            if schema in self.engine.preexisting:
                raise RuntimeError("schema already exists")
            self.engine.current_schemas.add(schema)
        elif sql.startswith("DROP SCHEMA "):
            schema = sql.removeprefix("DROP SCHEMA ").removesuffix(" CASCADE").strip('"')
            if self.engine.fail_drop:
                raise RuntimeError("simulated schema cleanup failure")
            self.engine.current_schemas.discard(schema)
            self.engine.dropped_schemas.add(schema)
        return None


class FakeEngine:
    def __init__(self, preexisting=(), fail_drop=False):
        self.statements = []
        self.preexisting = set(preexisting)
        self.current_schemas = set(preexisting)
        self.dropped_schemas = set()
        self.fail_drop = fail_drop

    @contextmanager
    def begin(self):
        yield FakeConnection(self)


def test_current_run_schema_is_cleaned_even_after_test_failure():
    engine = FakeEngine()
    name = new_schema_name()

    with pytest.raises(RuntimeError, match="simulated test failure"):
        with temporary_owned_schema(engine, name):
            assert name in engine.current_schemas
            raise RuntimeError("simulated test failure")

    assert engine.dropped_schemas == {name}
    assert name not in engine.current_schemas
    assert engine.statements[-1] == f"DROP SCHEMA {name} CASCADE"


def test_context_manager_yields_owned_schema_and_cleans_after_success():
    engine = FakeEngine()
    name = new_schema_name()

    with temporary_owned_schema(engine, name) as yielded_name:
        assert yielded_name == name
        assert name in engine.current_schemas
        assert name not in engine.dropped_schemas

    assert name in engine.dropped_schemas
    assert name not in engine.current_schemas


def test_cleanup_failure_is_reported_when_test_body_succeeds():
    name = new_schema_name()
    engine = FakeEngine(fail_drop=True)

    with pytest.raises(RuntimeError, match="simulated schema cleanup failure"):
        with temporary_owned_schema(engine, name):
            pass

    assert name in engine.current_schemas
    assert len([sql for sql in engine.statements if sql.startswith("DROP SCHEMA")]) == 1


def test_original_test_failure_is_preserved_when_cleanup_also_fails():
    name = new_schema_name()
    engine = FakeEngine(fail_drop=True)

    with pytest.raises(BaseExceptionGroup) as raised:
        with temporary_owned_schema(engine, name):
            raise ValueError("original test failure")

    leaves = raised.value.exceptions
    assert any(isinstance(error, ValueError) and str(error) == "original test failure" for error in leaves)
    assert any(
        isinstance(error, RuntimeError) and str(error) == "simulated schema cleanup failure"
        for error in leaves
    )
    assert name in engine.current_schemas


def test_preexisting_schema_collision_is_not_owned_or_dropped():
    name = new_schema_name()
    engine = FakeEngine(preexisting={name})

    with pytest.raises(RuntimeError, match="schema already exists"):
        with temporary_owned_schema(engine, name):
            pytest.fail("A pre-existing schema must not be entered as test-owned")

    assert name in engine.current_schemas
    assert name not in engine.dropped_schemas
    assert not any(statement.startswith("DROP SCHEMA") for statement in engine.statements)


def test_public_and_non_test_schema_names_are_rejected_before_ddl():
    engine = FakeEngine(preexisting={"public"})
    for unsafe_name in ("public", "neondb_owner", "nexa_test_arbitrary", "nexa_test_../public"):
        with pytest.raises(ValueError):
            with temporary_owned_schema(engine, unsafe_name):
                pass
    assert engine.statements == []
    assert engine.current_schemas == {"public"}
    assert engine.dropped_schemas == set()


def test_one_run_cannot_clean_another_runs_schema():
    engine = FakeEngine()
    first_name = new_schema_name()
    second_name = new_schema_name()

    with temporary_owned_schema(engine, first_name):
        with temporary_owned_schema(engine, second_name):
            assert {first_name, second_name} <= engine.current_schemas
        assert second_name in engine.dropped_schemas
        assert first_name in engine.current_schemas
        assert first_name not in engine.dropped_schemas

    assert first_name in engine.dropped_schemas
    assert engine.current_schemas == set()


def test_owned_cleanup_does_not_remove_unrelated_schema():
    unrelated = "nexa_test_" + "f" * 32
    owned = new_schema_name()
    engine = FakeEngine(preexisting={unrelated})

    with temporary_owned_schema(engine, owned):
        assert unrelated in engine.current_schemas

    assert unrelated in engine.current_schemas
    assert unrelated not in engine.dropped_schemas
    assert owned in engine.dropped_schemas


def test_schema_names_are_unique_and_alembic_url_targets_only_owned_schema():
    first_name = new_schema_name()
    second_name = new_schema_name()
    assert first_name != second_name
    validate_owned_schema_name(first_name)
    url = url_for_schema(
        "postgresql+psycopg://user:password@unit-test.neon.tech/nexa_test?sslmode=require",
        first_name,
    )
    assert make_url(url).query.get("options") == f"-csearch_path={first_name}"
    assert make_url(url).query.get("options") != "-csearch_path=public"

    with pytest.raises(ValueError):
        url_for_schema(
            "postgresql+psycopg://user:password@unit-test.neon.tech/nexa_test?sslmode=require",
            "public",
        )
