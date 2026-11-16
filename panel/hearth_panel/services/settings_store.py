"""Runtime settings stored in the database (editable from Admin → Settings)."""

from __future__ import annotations

import logging
import secrets

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Setting

CURSEFORGE_KEY = "curseforge_api_key"
UPDATE_INTERVAL = "update_interval_minutes"
SECRET_KEY = "secret_key"

log = logging.getLogger("hearth.settings")


def get(db: Session, key: str) -> str | None:
    row = db.get(Setting, key)
    return row.value if row else None


def put(db: Session, key: str, value: str | None) -> None:
    row = db.get(Setting, key)
    if value is None:
        if row:
            db.delete(row)
        return
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


MIN_SECRET_LENGTH = 32


def secret_key(db: Session, settings: Settings) -> str:
    if settings.secret_key:
        if len(settings.secret_key) < MIN_SECRET_LENGTH:
            # Anyone who guesses it can sign themselves in as any user. Warn rather than refuse,
            # so an existing install keeps running until the key is replaced.
            log.warning("HEARTH_SECRET_KEY is only %d characters. Sign-in sessions can be forged if it's "
                        "guessed: replace it with a random value of %d+ characters (for example the output "
                        "of `openssl rand -hex 32`), or remove it to let Hearth generate one. Everyone "
                        "will need to sign in again afterwards.", len(settings.secret_key), MIN_SECRET_LENGTH)
        return settings.secret_key
    value = get(db, SECRET_KEY)
    if not value:
        value = secrets.token_urlsafe(48)
        put(db, SECRET_KEY, value)
        db.commit()
    return value


def curseforge_key(db: Session, settings: Settings) -> str | None:
    return get(db, CURSEFORGE_KEY) or settings.curseforge_api_key


def update_interval(db: Session, settings: Settings) -> int:
    raw = get(db, UPDATE_INTERVAL)
    try:
        return max(15, int(raw)) if raw else settings.update_interval_minutes
    except ValueError:
        return settings.update_interval_minutes


TIMEZONE = "timezone"


def timezone_name(db: Session, settings: Settings) -> str:
    import os

    return get(db, TIMEZONE) or os.environ.get("TZ") or "UTC"
