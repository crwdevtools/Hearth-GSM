"""Is a password in known data breaches? (Have I Been Pwned's k-anonymity range API.)

Only the first 5 characters of the password's SHA-1 hash are sent; the service answers with every
breached hash starting with them and the match is made here, so neither the password nor its full
hash leaves the panel. If the service can't be reached the password is allowed: an outage there
shouldn't stop anyone from setting a password."""

from __future__ import annotations

import hashlib
import logging

from fastapi import HTTPException

log = logging.getLogger("hearth.pwned")

RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"


async def breach_count(ctx, password: str) -> int | None:
    """How many times the password appears in breaches; None when the check couldn't run."""
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()  # noqa: S324 (the API's format)
    prefix, suffix = digest[:5], digest[5:]
    try:
        r = await ctx.http.client.get(RANGE_URL.format(prefix=prefix), timeout=4,
                                      headers={"Add-Padding": "true"})
        r.raise_for_status()
    except Exception as exc:
        log.info("breached-password check skipped: %s", exc)
        return None
    for line in r.text.splitlines():
        candidate, _, count = line.strip().partition(":")
        if candidate == suffix:
            return int(count or 0)
    return 0


async def reject_breached(ctx, password: str) -> None:
    if not ctx.settings.breached_password_check:
        return
    count = await breach_count(ctx, password)
    if count:
        raise HTTPException(400, f"That password has appeared in {count:,} data breaches, so it's one of the first "
                                 "that attackers try. Choose a different one.")


def reject_breached_sync(ctx, password: str) -> None:
    """For plain `def` routes, which run in a worker thread: run the check on the event loop."""
    import anyio.from_thread

    anyio.from_thread.run(reject_breached, ctx, password)
