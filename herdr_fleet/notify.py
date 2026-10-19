"""``herdr notification show`` wrapper (design section 7, "Output").

Plugin stdout is shown nowhere, so actions invoked with ``--notify`` also send
their summary as a herdr notification. Seats blocked at startup always notify
with the ``request`` sound. Notification failures never fail an operation.
"""

from __future__ import annotations

from typing import Optional, TextIO

from herdr_fleet.herdr import Herdr, HerdrError, describe

MAX_BODY = 600


def notify(herdr: Herdr, title: str, body: str, sound: str = "done",
           errors: Optional[TextIO] = None) -> bool:
    """Show a notification; return False (and note why on ``errors``) on failure."""
    body = body.strip()
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 1].rstrip() + "…"
    try:
        herdr.notification_show(title, body or title, sound)
        return True
    except HerdrError as exc:
        if errors is not None:
            errors.write("herdr-fleet: notification failed: %s\n" % describe(exc))
        return False
