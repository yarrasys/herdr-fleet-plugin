"""Semantic ordinary-operation scopes; native assertions remain in herdr.py."""
from __future__ import annotations

import copy
import functools
import inspect
from contextlib import contextmanager

from herdr_fleet.herdr import HerdrError
from herdr_fleet import permits


def signature(agent):
    return tuple(agent.get(k) for k in ("name", "pane_id", "terminal_id", "workspace_id"))


def plan(ctx, names, topology=False, caller=False):
    """Plan before acquiring; selected names and affected workspace geometry only."""
    live = ctx.herdr.agent_list()
    chosen = set(names)
    values = [("N", n) for n in chosen]
    observed = {}
    workspaces = set()
    for agent in live:
        if agent.get("name") in chosen:
            if agent["name"] in observed:
                raise HerdrError("target_changed", "native name/pane mapping is ambiguous")
            values.extend(ctx.herdr.agent_resources(agent))
            workspaces.add(agent["workspace_id"])
            observed[agent["name"]] = signature(agent)
    for name in chosen - set(observed):
        pane = ctx.state.pane_of(name)
        if pane:
            try:
                record = ctx.herdr.pane_get(pane)
            except HerdrError as exc:
                if "not_found" not in exc.code:
                    raise
            else:
                values.extend(ctx.herdr.agent_resources(dict(record, name=None)))
                workspaces.add(record["workspace_id"])
        observed[name] = None
    if topology:
        for workspace in ctx.herdr.workspace_list():
            if (workspace.get("label") == ctx.fleet.workspace
                    or (workspace.get("workspace_id") == ctx.invocation.workspace_id and (caller or not ctx.fleet.workspace))
                    or workspace.get("workspace_id") == ctx.state.workspace_id):
                workspaces.add(workspace["workspace_id"])
        # Adoption/join may rebind the caller's formerly named terminal.
        for agent in live:
            if caller and agent.get("pane_id") == ctx.invocation.pane_id:
                values.extend(ctx.herdr.agent_resources(agent))
        for workspace in sorted(workspaces):
            for agent in live:
                if agent.get("workspace_id") == workspace:
                    values.extend(ctx.herdr.agent_resources(agent))
            for pane in ctx.herdr.pane_list(workspace):
                values.extend(ctx.herdr.agent_resources(dict(pane, name=None)))
    values.extend(("W", w) for w in workspaces)
    return values, observed


@contextmanager
def operation(ctx, names, topology=False, caller=False):
    if ctx.dry_run:
        yield ctx
        return
    values, observed = plan(ctx, names, topology, caller)
    panes = {value: (ctx.herdr._panes[value].get("workspace_id"), ctx.herdr._panes[value].get("terminal_id"))
             for kind, value in values if kind == "P" and value in ctx.herdr._panes}
    if not values:
        # A topology helper without seats still needs its exact caller workspace.
        workspace = ctx.invocation.workspace_id or ctx.state.workspace_id
        if not workspace:
            raise HerdrError("permit_unavailable", "no resources for this native operation")
        values = [("W", workspace)]
    with ctx.herdr.operation(values, topology) as native:
        records = native.agent_list()
        if any(sum(a.get("name") == name for a in records) > 1 for name in observed):
            raise HerdrError("target_changed", "native mapping became ambiguous during acquisition")
        current = {a.get("name"): signature(a) for a in records}
        if any(current.get(n) != value for n, value in observed.items()):
            raise HerdrError("target_changed", "target mapping changed while acquiring operation permits")
        if topology:
            for pane, before in panes.items():
                after = native.pane_get(pane)
                if (after.get("workspace_id"), after.get("terminal_id")) != before:
                    raise HerdrError("target_changed", "topology mapping changed while acquiring operation permits")
        bound = copy.copy(ctx)
        bound.herdr = native
        yield bound


def semantic(topology=False):
    """Outer lifecycle scopes pass the bound context to every nested helper."""
    def decorate(fn):
        parameters = inspect.signature(fn)

        @functools.wraps(fn)
        def run(ctx, *args, **kwargs):
            if ctx.dry_run:
                return fn(ctx, *args, **kwargs)
            supplied = parameters.bind(ctx, *args, **kwargs)
            named = supplied.arguments.get("names") or ([supplied.arguments["name"]] if "name" in supplied.arguments else [])
            names = list(named) if named else [s.name for s in ctx.fleet.seats]
            if supplied.arguments.get("me"):
                names.append(supplied.arguments["me"])
            if (fn.__name__ in ("op_restart", "op_swap", "op_down") and ctx.herdr.permit is None
                    and supplied.arguments.get("wait_ms")):
                ctx = preliminary_wait(ctx, names, supplied.arguments["wait_ms"], fn.__name__, supplied.arguments)
            shape = topology or supplied.arguments.get("close_panes", False) or supplied.arguments.get("close_workspace", False)
            if fn.__name__ in ("op_restart", "op_swap"):
                live = {a.get("name") for a in ctx.herdr.agent_list()}
                for name in names:
                    if name in live:
                        continue
                    pane = ctx.state.pane_of(name)
                    try:
                        if not pane:
                            raise HerdrError("pane_not_found", "no recorded pane")
                        ctx.herdr.pane_get(pane)
                    except HerdrError:
                        shape = True
            caller = bool(supplied.arguments.get("me")) or fn.__name__ == "op_ui"
            with operation(ctx, names, bool(shape), caller) as bound:
                result = fn(bound, *args, **kwargs)
            if (ctx.herdr.permit is None and fn.__name__ in ("op_up", "op_restart", "op_swap", "op_kickoff", "op_tile")
                    and supplied.arguments.get("name_panes", True)):
                from herdr_fleet import labels
                tail = []
                while result.results and result.results[-1].name == "layout":
                    tail.append(result.results.pop())
                labels.apply(ctx, None, result)
                result.results.extend(reversed(tail))
            return result
        return run
    return decorate


def preliminary_wait(ctx, names, wait_ms, operation_name, arguments):
    """User waiting is observation, outside ALL semantic submission permits."""
    from herdr_fleet.ops import _caller_refusal, WAIT_UNTIL
    from herdr_fleet.checks import drift_reasons
    prepared = copy.copy(ctx)
    prepared.preliminary_waits = {}
    live = {a.get("name"): a for a in ctx.herdr.agent_list()}
    for name in names:
        agent = live.get(name)
        if not agent or _caller_refusal(ctx, name, agent) or agent.get("agent_status") != "working":
            continue
        if operation_name == "op_swap" and not arguments.get("fallback"):
            seat = ctx.fleet.seat(name)
            if seat is not None and not drift_reasons(ctx, seat, agent):
                continue
        try:
            ctx.herdr.agent_wait(name, WAIT_UNTIL, wait_ms)
            prepared.preliminary_waits[name] = None
        except HerdrError as exc:
            prepared.preliminary_waits[name] = exc
    return prepared


@contextmanager
def consume(ctx, name):
    """Pure explicit-name attribution: never a native lookup or endpoint probe."""
    if name == "owner" or ctx.dry_run:
        yield
        return
    try:
        with permits.Store().acquire(permits.endpoints(ctx.env), [("N", name)]):
            yield
    except permits.Held:
        raise HerdrError("owner_control", "owner control; agent mailbox remains queued")
    except permits.Unavailable as exc:
        raise HerdrError("permit_unavailable", str(exc))
