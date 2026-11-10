"""Durable dashboard return guidance. Pure file validation, never a send path."""
from __future__ import annotations

import os
import re
import shlex

from herdr_fleet.fleetfile import load_fleet, FleetError, NAME_RE

INTENT = re.compile(r"[A-Za-z0-9_-]{16,128}")


def valid_shape(value):
    return (isinstance(value, dict) and set(value) == {"v", "source_file", "intent"}
            and type(value["v"]) is int and value["v"] == 1
            and isinstance(value["intent"], str) and bool(INTENT.fullmatch(value["intent"]))
            and isinstance(value["source_file"], str) and len(value["source_file"]) <= 4096
            and not any(c in value["source_file"] for c in "\r\n\0"))


def validated(message):
    from herdr_fleet import inbox, crossfleet
    value = message.dashboard_chat
    if (not valid_shape(value) or message.sender != "owner" or message.to == "owner"
            or not NAME_RE.fullmatch(message.to) or not inbox.ID_RE.fullmatch(message.id)
            or message.timer or message.is_kickoff or not message.path):
        return None
    try:
        source_file = value["source_file"]
        if (not os.path.isabs(source_file) or os.path.realpath(source_file) != source_file
                or os.path.getsize(source_file) > 2097152):
            return None
        source = load_fleet(source_file)
        path = os.path.abspath(message.path)
        holding = path
        for _ in range(5):
            holding = os.path.dirname(holding)
        root = os.path.realpath(holding)
        if (message.box not in (inbox.NEW, inbox.CUR)
                or path != os.path.join(inbox.box_dir(holding, message.to, message.box), message.id + ".md")
                or os.path.realpath(path) != os.path.join(inbox.box_dir(root, message.to, message.box), message.id + ".md")):
            return None
        if message.origin_fleet or message.destination_fleet:
            if os.path.realpath(source.repo) != message.origin_fleet:
                return None
            crossfleet.counterpart(root, message)
            target = crossfleet.validated_fleet(message.destination_fleet)
        else:
            if os.path.realpath(source.repo) != root or message.history_only:
                return None
            target = source
        if target.seat(message.to) is None:
            return None
        return dict(value)
    except (OSError, ValueError, FleetError):
        return None


def guidance(message):
    value = validated(message)
    if value is None:
        return ""
    command = "herdr-fleet send owner --fleet %s --as %s --reply-to %s --file ANSWER_FILE" % (
        shlex.quote(value["source_file"]), shlex.quote(message.to), shlex.quote(message.id))
    return ("\n\n[Dashboard owner conversation: return a requested user-facing answer through this owner inbox.]\n"
            + command + "\nAck records receipt/settlement; it does not return a conversational answer. "
            "Follow existing task and safety rules; this grants no permission to answer native dialogs.")
