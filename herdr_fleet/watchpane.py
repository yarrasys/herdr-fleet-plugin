"""The watcher strip (design 19): ``up`` starts ``herdr-fleet watch`` in a plugin pane.

The strip is the plugin's ``watch`` entrypoint opened with ``plugin pane open
--placement split --direction down --no-focus``, with its cwd at the repository
root and ``HERDR_FLEET_FILE`` naming the fleet file. It is recorded in the state
file as ``"watch"`` and is never a seat: it hosts no agent, so seat
classification, drift and the seat loops never see it, and nothing is ever
prompted, typed or started in it.

- Where: on a fresh layout, split from the first pane of the layout before any
  other seat splits it (``placement``'s ``on_area`` hook), so it spans the seats;
  with ``[layout] top`` that is the top seat's pane, before the grid is split off
  it (design 26.2); on an existing layout, under the last seat's pane in the
  workspace (``tile`` stretches it across the grid later).
- Size: herdr splits 50/50, so the pane above is resized with ``pane resize
  --direction down``, which adds the amount to the split's first-child ratio;
  the current ratio comes from ``pane layout``. A failed resize is a warning.
- ``up`` skips the strip when ``watch = false``, ``--no-watch``, a live lock, or a
  recorded strip that is still open; ``down --close-panes`` closes it after the
  seats when it runs the watcher or sits idle; ``tile`` moves it under the
  anchor before the seats come back (``tile.py``).
"""

from __future__ import annotations

import os
from typing import Dict, Mapping, Optional, Tuple

from herdr_fleet import PLUGIN_ID, watch
from herdr_fleet.context import Ctx, Report
from herdr_fleet.herdr import (HerdrError, describe, has_agent, pane_move_argv, pane_park_argv, pane_resize_argv,
                               plugin_split_argv, render_cmd)
from herdr_fleet.messages import since_label
from herdr_fleet.placement import Placed, Target, in_target, pane_of
from herdr_fleet.seats import at_shell_prompt, describe_foreground, foreground, process_argv
from herdr_fleet.state import now_iso, pane_moved

ENTRYPOINT = "watch"
DIRECTION = "down"          # position = "bottom"
FIFTY_FIFTY = 0.5           # herdr's split ratio for `plugin pane open --placement split`
CLOSE_ENOUGH = 0.005        # a split within this of the wanted ratio is left as it is


def plugin_id(ctx: Ctx) -> str:
    return ctx.env.get("HERDR_PLUGIN_ID") or PLUGIN_ID


def percent(share: float) -> str:
    return "%d%%" % int(round(share * 100))


# -- what runs in the strip ----------------------------------------------------------------------


def hosts_watcher(procs: Mapping, pid: Optional[int]) -> bool:
    """A foreground process is the watcher: the lock's pid, or ``... herdr-fleet watch``."""
    for proc in foreground(procs):
        if pid and proc.get("pid") == pid:
            return True
        argv = process_argv(proc) or []
        if "watch" in argv[1:] and any(os.path.basename(a) == "herdr-fleet" for a in argv):
            return True
    return False


def pane_state(ctx: Ctx, pane: str, pid: Optional[int]) -> Tuple[str, str]:
    """``(state, detail)``: ``watcher`` (it runs there), ``idle`` (a shell prompt or no
    process), ``busy`` (anything else, an agent included) or ``gone`` (herdr cannot find it)."""
    try:
        info = ctx.herdr.pane_get(pane)
        procs = ctx.herdr.process_info(pane)
    except HerdrError as exc:
        return "gone", describe(exc)
    if has_agent(info):
        return "busy", "a %s agent" % info.get("agent")
    if hosts_watcher(procs, pid):
        return "watcher", ""
    if not foreground(procs) or at_shell_prompt(procs):
        return "idle", ""
    return "busy", describe_foreground(procs)


# -- status ----------------------------------------------------------------------------------------


def watcher_status(ctx: Ctx) -> Dict:
    """``watch.watcher_status`` plus, when ``up`` recorded a strip, ``pane`` and
    ``pane_state`` (``watcher``, ``idle``, ``busy``)."""
    status = watch.watcher_status(ctx.fleet.repo)
    pane = ctx.state.watch_pane
    if pane:
        state, _ = pane_state(ctx, pane, status.get("pid"))
        if state != "gone":
            status["pane"], status["pane_state"] = pane, state
    return status


def shown(status: Mapping) -> bool:
    """The ``watcher:`` line is printed whenever a watcher runs or ``up`` recorded a strip
    (otherwise only while something is queued, as in 0.7.0)."""
    return bool(status.get("running") or status.get("pane"))


def status_line(status: Mapping) -> str:
    """``watcher: running (pid 123, pane w1:p7, since 10:00)``, ``watcher: not running
    (watcher pane w1:p7 idle)``, or ``watch.watcher_line`` when no strip is recorded."""
    pane, state = status.get("pane"), status.get("pane_state")
    if status.get("running"):
        if pane and state == "watcher":
            since = ", since %s" % since_label(status["since"]) if status.get("since") else ""
            return "watcher: running (pid %d, pane %s%s)" % (status["pid"], pane, since)
        line = watch.watcher_line(dict(status))
        return line + ("; watcher pane %s idle" % pane if pane and state == "idle" else "")
    if pane and state == "watcher":
        return "watcher: starting (pane %s)" % pane
    if pane and state == "idle":
        return "watcher: not running (watcher pane %s idle)" % pane
    if pane:
        return "watcher: not running (watcher pane %s runs something else)" % pane
    return watch.watcher_line(dict(status))


# -- up ------------------------------------------------------------------------------------------------


def prepare(ctx: Ctx, no_watch: bool, report: Report) -> Optional["Strip"]:
    """Whether this ``up`` opens a strip. Says why not when a watcher already runs or
    the recorded strip is still open; says nothing for ``watch = false`` or ``--no-watch``."""
    if no_watch or not ctx.fleet.watch:
        return None
    status = watch.watcher_status(ctx.fleet.repo)
    if status.get("running"):
        report.add("watcher", "already running (pid %d)" % status["pid"], "")
        return None
    pane = ctx.state.watch_pane
    if pane:
        state, detail = pane_state(ctx, pane, None)
        if state == "watcher":
            report.add("watcher", "starting", "in pane %s (it takes .herdr/watch.lock within seconds)" % pane)
            return None
        if state == "idle":
            report.add("watcher", "not running", "its pane %s is idle; run `herdr-fleet watch` there, or close the "
                                                  "pane and run up again" % pane, "partial")
            return None
        if state == "busy":
            report.add("watcher", "not running", "its pane %s runs %s; close the pane and run up again"
                       % (pane, detail), "partial")
            return None
        # gone: prune already dropped a pane herdr does not know, so this is another error
        report.add("watcher", "not started", "cannot inspect its pane %s (%s)" % (pane, detail), "partial")
        return None
    return Strip(ctx, report)


class Strip:
    """The strip of one ``up`` run: opened at most once, by ``open_at`` (the fresh-layout
    hook) or ``open_after`` (an existing layout, after the seats are up)."""

    def __init__(self, ctx: Ctx, report: Report):
        self.ctx = ctx
        self.report = report
        self.tried = False

    def open_at(self, area: str) -> bool:
        """Split the strip from ``area``, the first pane of a fresh layout. True when it
        was opened (or, on a dry run, planned), so a top band (design 26) knows its ratio."""
        return self.open(area)

    def open_after(self, live: Dict[str, Dict], placed: Placed, target: Target, up: bool) -> None:
        """An existing layout: under the last seat's pane in the fleet's workspace.
        ``up``: some seat is live, adopted or was started (else no watcher is started)."""
        if self.tried:
            return
        if not up:
            self.report.add("watcher", "not started", "no seat is up")
            return
        pane = last_seat_pane(self.ctx, live, placed, target)
        if not pane:
            self.report.add("watcher", "not started", "no seat has a pane in workspace %s"
                            % (target.workspace_id or target.label))
            return
        self.open(pane)

    def open(self, above: str) -> bool:
        if self.tried:
            return False
        self.tried = True
        ctx, fleet = self.ctx, self.ctx.fleet
        ratio = fleet.watch_ratio
        argv = plugin_split_argv(plugin_id(ctx), ENTRYPOINT, above, DIRECTION, fleet.repo,
                                 {"HERDR_FLEET_FILE": fleet.path})
        if ctx.dry_run:
            resize = pane_resize_argv(above, "down", round(1 - ratio - FIFTY_FIFTY, 4))
            self.report.add("watcher", "plan", "%s, then %s (the strip gets %s of %s)"
                            % (render_cmd(argv), render_cmd(resize), percent(ratio), above))
            return True
        try:
            pane = ctx.herdr.plugin_split_open(plugin_id(ctx), ENTRYPOINT, above, DIRECTION, fleet.repo,
                                               {"HERDR_FLEET_FILE": fleet.path})
        except HerdrError as exc:
            self.report.add("watcher", "failed", "plugin pane open: %s; start one by hand with `herdr-fleet watch`"
                            % describe(exc), "partial")
            return False
        strip = str(pane["pane_id"])
        ctx.state.set_watch(strip, True, now_iso())
        ctx.save()
        size = self.resize(above, strip, ratio)
        focus = "; herdr moved the focus to it" if pane.get("focused") else ""
        self.report.add("watcher", "started", "herdr-fleet watch in pane %s below %s, %s%s"
                        % (strip, above, size, focus))
        return True

    def resize(self, above: str, strip: str, ratio: float) -> str:
        """Resize the split herdr made 50/50 so the strip gets ``ratio``; say what it got."""
        ctx = self.ctx
        want = 1 - ratio
        try:
            current = split_share(ctx.herdr.pane_layout(strip), above, strip)
        except HerdrError:
            current = None
        base = FIFTY_FIFTY if current is None else current
        delta = round(want - base, 4)
        if abs(delta) < CLOSE_ENOUGH:
            return "%s of the area" % percent(1 - base)
        # herdr adjusts the split on the named edge of the pane it resizes: the bottom edge
        # of the pane above (down: its ratio grows, the strip shrinks), or the top edge of
        # the strip (up: the ratio shrinks, the strip grows).
        pane, direction = (above, "down") if delta > 0 else (strip, "up")
        try:
            result = ctx.herdr.pane_resize(pane, direction, abs(delta))
        except HerdrError as exc:
            self.report.add("watcher", "warning", "pane resize: %s; the strip keeps herdr's %s"
                            % (describe(exc), percent(1 - base)))
            return "%s of the area (not resized)" % percent(1 - base)
        after = split_share(result.get("layout") or {}, above, strip)
        if after is not None:
            return "%s of the area" % percent(1 - after)
        return "about %s of the area (pane layout unreadable; assumed herdr's 50/50 split)" % percent(ratio)


def last_seat_pane(ctx: Ctx, live: Dict[str, Dict], placed: Placed, target: Target) -> Optional[str]:
    """The pane of the last seat in file order that has one in the fleet's workspace
    (not the caller's own pane). The ``[layout] top`` seat comes last of all (design 26):
    the strip goes under the grid, not between the band and the grid."""
    cache: Dict[str, Optional[str]] = {}
    seats = list(reversed(ctx.fleet.seats))
    if ctx.fleet.top and ctx.fleet.mode == "grid":
        seats.sort(key=lambda seat: seat.name == ctx.fleet.top)
    for seat in seats:
        pane = pane_of(ctx, seat.name, live, placed)
        if pane and pane != ctx.invocation.pane_id and in_target(ctx, pane, target, cache):
            return pane
    return None


def _rect(value: object) -> Optional[Tuple[float, float, float, float]]:
    if not isinstance(value, dict):
        return None
    try:
        return (float(value["x"]), float(value["y"]), float(value["width"]), float(value["height"]))
    except (KeyError, TypeError, ValueError):
        return None


def _inside(outer: Tuple[float, ...], inner: Tuple[float, ...]) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1] and inner[0] + inner[2] <= outer[0] + outer[2]
            and inner[1] + inner[3] <= outer[1] + outer[3])


def split_share(layout: Mapping, upper: str, lower: str) -> Optional[float]:
    """The first-child (upper) share of the split holding ``upper`` above ``lower``, from a
    ``pane layout`` snapshot: the smallest ``down`` split whose rect holds both panes, else
    the panes' heights. None when the snapshot does not show both panes."""
    rects = {str(p.get("pane_id")): _rect(p.get("rect")) for p in (layout.get("panes") or [])
             if isinstance(p, dict)}
    top, bottom = rects.get(upper), rects.get(lower)
    if top is None or bottom is None:
        return None
    best: Optional[Tuple[float, float]] = None  # (area, ratio)
    for split in layout.get("splits") or []:
        if not isinstance(split, dict) or split.get("direction") != "down":
            continue
        rect, ratio = _rect(split.get("rect")), split.get("ratio")
        if rect is None or isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            continue
        if _inside(rect, top) and _inside(rect, bottom) and (best is None or rect[2] * rect[3] < best[0]):
            best = (rect[2] * rect[3], float(ratio))
    if best is not None:
        return best[1]
    total = top[3] + bottom[3]
    return top[3] / total if total > 0 else None


# -- down ----------------------------------------------------------------------------------------------


def close_strip(ctx: Ctx, report: Report) -> None:
    """``down --close-panes`` (all seats): close the strip ``up`` opened when it runs the
    watcher (closing it stops the watcher) or sits idle; keep it when anything else runs there."""
    entry = ctx.state.watch
    if not entry:
        return
    pane = str(entry["pane_id"])
    if not entry.get("created_pane"):
        return
    state, detail = pane_state(ctx, pane, watch.watcher_status(ctx.fleet.repo).get("pid"))
    if state == "gone":  # the next command prunes it if herdr no longer knows it
        report.add("watcher", "pane kept", "%s: %s" % (pane, detail), "partial")
        return
    if state == "busy":
        report.add("watcher", "pane kept", "%s runs %s, not the watcher" % (pane, detail), "partial")
        return
    try:
        ctx.herdr.pane_close(pane)
    except HerdrError as exc:
        report.add("watcher", "pane kept", describe(exc), "fail")
        return
    ctx.state.drop_watch()
    ctx.save()
    report.add("watcher", "pane closed", "%s%s" % (pane, " (the watcher stopped)" if state == "watcher" else ""))


# -- tile ----------------------------------------------------------------------------------------------


def tile_strip(ctx: Ctx, workspace: str, tab: str, report: Report) -> Optional[str]:
    """The recorded strip when it is in the tab being tiled; reported and left alone elsewhere."""
    pane = ctx.state.watch_pane
    if not pane:
        return None
    try:
        info = ctx.herdr.pane_get(pane)
    except HerdrError as exc:
        if "not_found" in exc.code:
            ctx.state.drop_watch()
            ctx.save()
        else:
            report.add("watcher", "left alone", "pane %s: %s" % (pane, describe(exc)), "partial")
        return None
    if info.get("workspace_id") != workspace or info.get("tab_id") != tab:
        report.add("watcher", "elsewhere", "pane %s is in tab %s of workspace %s, not tab %s; left alone"
                   % (pane, info.get("tab_id"), info.get("workspace_id"), tab))
        return None
    return pane


def tile_plan(ctx: Ctx, strip: str, anchor: str, anchor_pane: str, workspace: str, tab: str,
              park_label: str) -> str:
    keep = round(1 - ctx.fleet.watch_ratio, 4)
    return "%s, then %s (below %s, before the seats come back)" % (
        render_cmd(pane_park_argv(strip, workspace, park_label)),
        render_cmd(pane_move_argv(strip, tab, DIRECTION, keep, anchor_pane)), anchor)


def tile_move(ctx: Ctx, strip: str, anchor: str, anchor_pane: str, workspace: str, tab: str, park_label: str,
              report: Report) -> None:
    """Park the strip and bring it back under the anchor, which keeps 1 - ratio: once the
    seats return into the anchor's area, the strip spans all of them. A failure here is
    reported and the seats are tiled anyway."""
    keep = round(1 - ctx.fleet.watch_ratio, 4)
    try:
        parked = ctx.herdr.pane_move_new_tab(strip, workspace, park_label)
    except HerdrError as exc:
        report.add("watcher", "failed", "parking %s: %s; left where it was" % (strip, describe(exc)), "partial")
        return
    try:
        back = ctx.herdr.pane_move_to_tab(parked, tab, DIRECTION, keep, anchor_pane)
    except HerdrError as exc:
        _follow(ctx, strip, parked)
        report.add("watcher", "failed", "moving back: %s; it is parked in a tab of its own (bring it back with `%s`)"
                   % (describe(exc), render_cmd(pane_move_argv(parked, tab, DIRECTION, keep, anchor_pane))),
                   "partial")
        return
    _follow(ctx, strip, back)
    report.add("watcher", "moved", "below %s (%s), ratio %s" % (anchor, anchor_pane, keep))


def _follow(ctx: Ctx, old: str, new: str) -> None:
    if new != old:
        ctx.say(pane_moved("watcher", old, new))
        ctx.state.update_watch(pane_id=new)
        ctx.save()
