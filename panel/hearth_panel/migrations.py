"""Bring an existing database up to the current models without losing data.

Hearth's schema changes are additive (new tables, new columns with defaults), so the upgrade is:
create missing tables, then ALTER TABLE ADD COLUMN for every model column the database lacks.
Runs on every start and is a no-op when nothing changed.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn

from .db import Base

log = logging.getLogger("hearth.migrations")


def upgrade(engine: Engine) -> list[str]:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    added: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable and column.server_default is None and not column.primary_key:
                    raise RuntimeError(
                        f"Can't add {table.name}.{column.name} automatically: it is NOT NULL without a "
                        "server default. Give the model column a server_default."
                    )
                ddl = CreateColumn(column).compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
                added.append(f"{table.name}.{column.name}")
    if added:
        log.info("database upgraded: added %s", ", ".join(added))
    return added
