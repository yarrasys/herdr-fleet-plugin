"""Cross-fleet sends logged on both sides (design 20.8, item 3b).

When ``send`` targets a live agent that is neither a seat nor a contact of this
fleet, and that agent works in another fleet's repository on this machine, the
``send`` entry (same id, same fields) is also appended to that fleet's
``.herdr/messages.jsonl`` and the body stored as that fleet's
``.herdr/inbox/<target>/cur/<id>.md``, so its ``status``, ``log --owed`` and
``inbox --history`` see the message and the debt a reply settles. A message
queued here and delivered later also gets its ``delivered`` entry there.

The other fleet is found from the agent record's ``cwd``, then its
``foreground_cwd``: the first directory walking up that holds
``.herdr/fleet.toml`` (no ``HERDR_FLEET_FILE``, no ``--fleet``). A path that
resolves to this fleet's own repository is not another fleet. Nothing is
created unless that ``.herdr/`` directory already exists; files follow the
inbox's atomic rules (temp file and rename, never overwritten) and the log is
appended with ``O_APPEND``. Nothing is ever prompted from here.
"""

from __future__ import annotations

import copy
import os
from typing import Dict, Mapping, Optional, Sequence

from herdr_fleet import inbox
from herdr_fleet.fleetfile import FLEET_DIR, FLEET_NAME
from herdr_fleet.inbox import CUR, Message


def fleet_root(start: object) -> Optional[str]:
    """The repository root of the first fleet at or above ``start``, or None."""
    if not isinstance(start, str) or not start or not os.path.isdir(start):
        return None
    folder = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(folder, FLEET_DIR, FLEET_NAME)):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def other_fleet(agent: Optional[Mapping], own_repo: str) -> Optional[str]:
    """The other fleet's repository for a live agent record: from ``cwd``, then
    ``foreground_cwd``; None when neither leads to a fleet, or it is this one."""
    if not agent:
        return None
    own = os.path.realpath(own_repo)
    for key in ("cwd", "foreground_cwd"):
        root = fleet_root(agent.get(key))
        if root is None:
            continue
        return None if os.path.realpath(root) == own else root
    return None


def find(agents: Sequence[Mapping], name: str, own_repo: str) -> Optional[str]:
    """``other_fleet`` for the live agent named ``name``."""
    for agent in agents:
        if agent.get("name") == name:
            return other_fleet(agent, own_repo)
    return None


def _herdr_dir(repo: str) -> Optional[str]:
    folder = os.path.join(repo, FLEET_DIR)
    return folder if os.path.isdir(folder) else None


def mirror_entry(repo: str, entry: Dict) -> None:
    """Append one log entry to the other fleet's message log (only when its ``.herdr/``
    exists). Raises OSError; the caller turns it into a warning."""
    from herdr_fleet.messages import append  # the log's own O_APPEND writer
    if _herdr_dir(repo) is None:
        raise OSError("%s has no %s directory" % (repo, FLEET_DIR))
    append(repo, entry)


def mirror_body(repo: str, message: Message) -> None:
    """Store the message file in the other fleet's ``inbox/<to>/cur/`` (atomically, as
    ``inbox.write``), unless a file with that id is already there."""
    if _herdr_dir(repo) is None:
        raise OSError("%s has no %s directory" % (repo, FLEET_DIR))
    target = os.path.join(inbox.box_dir(repo, message.to, CUR), message.id + ".md")
    if os.path.lexists(target):
        return
    inbox.write(repo, copy.copy(message), CUR)
