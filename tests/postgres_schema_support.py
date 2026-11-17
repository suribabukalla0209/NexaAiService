"""Safe lifecycle helpers for PostgreSQL integration-test schemas only."""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url

_SCHEMA_NAME = re.compile(r"^nexa_test_[0-9a-f]{32}$")


def new_schema_name() -> str:
    return f"nexa_test_{uuid.uuid4().hex}"


def validate_owned_schema_name(schema_name: str) -> None:
    if not _SCHEMA_NAME.fullmatch(schema_name):
        raise ValueError("Refusing an invalid or non-test schema identifier")


def url_for_schema(database_url: str, schema_name: str) -> str:
    validate_owned_schema_name(schema_name)
    return make_url(database_url).update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    ).render_as_string(hide_password=False)


def _cleanup_owned_schemas(engine: Engine, owned_schemas: set[str]) -> None:
    cleanup_errors: list[BaseException] = []
    for owned_name in tuple(owned_schemas):
        try:
            validate_owned_schema_name(owned_name)
            with engine.begin() as connection:
                connection.execute(DropSchema(owned_name, cascade=True))
        except BaseException as exc:
            cleanup_errors.append(exc)
        else:
            owned_schemas.remove(owned_name)

    if cleanup_errors:
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        raise BaseExceptionGroup("Multiple owned test schemas failed cleanup", cleanup_errors)


@contextmanager
def temporary_owned_schema(engine: Engine, schema_name: str | None = None) -> Iterator[str]:
    """Create and drop only this invocation's uniquely named test schema.

    Ownership is recorded only after CREATE SCHEMA succeeds. If the identifier
    already exists, CREATE fails before it can enter the owned set, so cleanup
    cannot drop a pre-existing schema. Public and arbitrary schema names fail
    validation before any DDL is issued.
    """
    name = schema_name or new_schema_name()
    validate_owned_schema_name(name)
    owned_by_this_run: set[str] = set()
    with engine.begin() as connection:
        connection.execute(CreateSchema(name))
    # Register only after the CREATE transaction committed successfully.
    owned_by_this_run.add(name)

    try:
        yield name
    except BaseException as test_error:
        try:
            _cleanup_owned_schemas(engine, owned_by_this_run)
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "Test body and owned-schema cleanup both failed",
                [test_error, cleanup_error],
            ) from None
        raise
    else:
        _cleanup_owned_schemas(engine, owned_by_this_run)
    finally:
        owned_by_this_run.clear()
