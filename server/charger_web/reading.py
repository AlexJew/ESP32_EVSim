"""One reading in the shape the page expects: a value, a status and the time it was taken."""

import time
from datetime import datetime, timezone


def reading(value, at, now, stale_after, force_stale=False, wall=time.time):
    """`at` and `now` come from the same monotonic clock; `wall` turns the age into a UTC timestamp.

    unavailable: there is no value. stale: the value is `stale_after` seconds old or more, or has no
    time, or `force_stale` (a retained message, or a link that is down). ok: otherwise.
    """
    if value is None:
        return {"value": None, "status": "unavailable", "updated_at": None}
    if force_stale or at is None or now - at >= stale_after:
        status = "stale"
    else:
        status = "ok"
    updated_at = None
    if at is not None:
        updated_at = datetime.fromtimestamp(wall() - max(0.0, now - at), timezone.utc).isoformat()
    return {"value": value, "status": status, "updated_at": updated_at}
