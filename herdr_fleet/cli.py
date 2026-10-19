"""Command-line interface: argparse, dispatch, output and exit codes.

Exit codes: 0 ok, 1 failure, 2 usage error or invalid fleet file, 3 partial
(some seats skipped, refused or blocked). Expected failures (herdr errors,
fleet file errors, refusals) print one clear message and never a traceback.
With ``--notify`` (set by the manifest actions, whose stdout herdr shows
nowhere) the summary is also sent as a herdr notification.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Mapping, Optional, Sequence, TextIO

from herdr_fleet import PLUGIN_ROOT, __version__
from herdr_fleet.checks import collect_status, render_status, run_doctor
from herdr_fleet.context import (EXIT_FAIL, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, Ctx, OpError, Report,
                                 UsageError, preflight)
from herdr_fleet.fleetfile import FleetError
from herdr_fleet.herdr import Herdr, HerdrError, Runner, describe, find_binary
from herdr_fleet.menu import run_menu, wait_for_enter
from herdr_fleet.ops import op_down, op_install_cli, op_kickoff, op_restart, op_swap, op_ui, op_up
from herdr_fleet.scaffold import available_examples, run_init
from herdr_fleet.seats import Clock
from herdr_fleet.state import StateError
from herdr_fleet.tile import op_tile



def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--file", metavar="PATH",
                        help="fleet file to use instead of discovering .herdr/fleet.toml (also HERDR_FLEET_FILE)")
    common.add_argument("--notify", action="store_true",
                        help="also send the result as a herdr notification (the manifest actions set this)")
    notes = argparse.ArgumentParser(add_help=False)
    group = notes.add_mutually_exclusive_group()
    group.add_argument("--note", metavar="TEXT", help="text for {note} in the kickoff")
    group.add_argument("--note-file", metavar="PATH", help="file whose contents fill {note}")
    wait = argparse.ArgumentParser(add_help=False)
    wait.add_argument("--wait", metavar="MS", type=int,
                      help="let a working agent settle for up to MS milliseconds before quitting it")

    parser = argparse.ArgumentParser(
        prog="herdr-fleet",
        description="Start and manage a team of named coding agents in herdr from .herdr/fleet.toml.")
    parser.add_argument("--version", action="version", version="herdr-fleet %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("up", parents=[common], help="start every seat that is not running, then brief it")
    p.add_argument("names", nargs="*", metavar="NAME", help="only these seats (default: all)")
    p.add_argument("--me", metavar="NAME", help="adopt the agent in your own pane as seat NAME (rename only)")
    p.add_argument("--join", action="store_true",
                   help="with --me: move your pane into the fleet's workspace if it is elsewhere (herdr pane move)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    p.add_argument("--no-kickoff", action="store_true", help="start seats but send no kickoff")
    p.add_argument("--note", metavar="TEXT", help="text for {note} in the kickoff")

    p = sub.add_parser("status", parents=[common], help="show every seat, its state and drift")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    p.add_argument("--hold", action="store_true", help="wait for Enter before exiting (overlay pane)")

    p = sub.add_parser("restart", parents=[common, notes, wait],
                       help="quit idle seats and start them again in the same pane, then brief them")
    p.add_argument("names", nargs="+", metavar="NAME")
    p.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")

    p = sub.add_parser("swap", parents=[common, notes, wait],
                       help="restart exactly the seats whose kind or args drifted from the file")
    p.add_argument("names", nargs="*", metavar="NAME", help="only these seats (default: all)")
    p.add_argument("--dry-run", action="store_true", help="list what would change and why")

    p = sub.add_parser("kickoff", parents=[common, notes], help="resend the kickoff to idle seats")
    p.add_argument("names", nargs="+", metavar="NAME")

    p = sub.add_parser("down", parents=[common, wait], help="quit seats; optionally close what herdr-fleet created")
    p.add_argument("names", nargs="*", metavar="NAME", help="only these seats (default: all)")
    p.add_argument("--close-panes", action="store_true", help="close panes herdr-fleet created, once at a shell")
    p.add_argument("--close-workspace", action="store_true",
                   help="close the workspace if herdr-fleet created it and no agent is left")

    p = sub.add_parser("tile", parents=[common],
                       help="re-tile the live seats in place ([layout] grid or chain); moves panes only")
    p.add_argument("--dry-run", action="store_true", help="print the pane moves and change nothing")

    sub.add_parser("doctor", parents=[common], help="check python, herdr, the fleet file and config (read-only)")

    p = sub.add_parser("init", help="write a starter .herdr/fleet.toml (or copy an example) into a repository",
                       description="Write a starter .herdr/fleet.toml, kickoff template and state note at the root "
                                   "of DIR's Git work tree (else DIR itself). Never overwrites without --force. "
                                   "Runs no herdr command.")
    p.add_argument("dir", nargs="?", metavar="DIR", help="target directory (default: the current directory)")
    p.add_argument("--example", metavar="NAME",
                   help="copy examples/NAME as shipped instead (available: %s)" % ", ".join(available_examples()))
    p.add_argument("--force", action="store_true", help="overwrite files that already exist")
    p.add_argument("--notify", action="store_true", help="also send the result as a herdr notification")

    p = sub.add_parser("install-cli", help="symlink bin/herdr-fleet into DIR so agents can run it by name")
    p.add_argument("--dir", metavar="DIR", help="target directory (default ~/.local/bin)")

    p = sub.add_parser("ui", parents=[common], help="open the status overlay or manage popup (manifest actions)")
    p.add_argument("entrypoint", choices=("status", "manage"))

    sub.add_parser("menu", parents=[common], help="the numbered popup menu (manifest pane)")
    return parser


def main(argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None,
         runner: Optional[Runner] = None, clock: Optional[Clock] = None, stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None, stdin: Optional[TextIO] = None, cwd: Optional[str] = None) -> int:
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    stdin = stdin or sys.stdin
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else EXIT_USAGE
    if args.command == "install-cli":
        return op_install_cli(PLUGIN_ROOT, args.dir, stdout, env.get("PATH", ""))
    herdr = Herdr(find_binary(env), runner)
    ctx = Ctx(herdr, env, clock or Clock(), stdout, stderr, cwd or os.getcwd(), file=getattr(args, "file", None),
              notify=args.notify, dry_run=getattr(args, "dry_run", False))
    try:
        return _dispatch(ctx, args, stdin)
    except FleetError as exc:
        return _fail(ctx, args, stdin, EXIT_USAGE, "fleet file", exc.errors)
    except UsageError as exc:
        return _fail(ctx, args, stdin, EXIT_USAGE, "usage", [str(exc)])
    except (OpError, StateError) as exc:
        return _fail(ctx, args, stdin, EXIT_FAIL, "error", [str(exc)])
    except HerdrError as exc:
        return _fail(ctx, args, stdin, EXIT_FAIL, "error", [describe(exc)])
    except OSError as exc:
        return _fail(ctx, args, stdin, EXIT_FAIL, "error", [str(exc)])
    except KeyboardInterrupt:
        stderr.write("herdr-fleet: interrupted\n")
        return 130


def _dispatch(ctx: Ctx, args: argparse.Namespace, stdin: TextIO) -> int:
    command = args.command
    if command == "init":  # pure file writing: no preflight, no herdr command
        return _init(ctx, args)
    if command == "doctor":
        return _doctor(ctx)
    preflight(ctx)
    if command == "status":
        return _status(ctx, args.json, args.hold, stdin)
    if command == "ui":
        op_ui(ctx, args.entrypoint)
        return EXIT_OK
    if command == "menu":
        return run_menu(ctx, stdin)
    note = _note(args)
    if command == "up":
        report = op_up(ctx, args.names, me=args.me, no_kickoff=args.no_kickoff, note=args.note, join=args.join)
        return _finish(ctx, report, status_table=True)
    if command == "restart":
        return _finish(ctx, op_restart(ctx, args.names, note, args.wait))
    if command == "swap":
        return _finish(ctx, op_swap(ctx, args.names, note, args.wait))
    if command == "kickoff":
        return _finish(ctx, op_kickoff(ctx, args.names, note))
    if command == "down":
        return _finish(ctx, op_down(ctx, args.names, args.wait, args.close_panes, args.close_workspace))
    if command == "tile":
        return _finish(ctx, op_tile(ctx))
    raise UsageError("unknown command %s" % command)


def _note(args: argparse.Namespace) -> Optional[str]:
    path = getattr(args, "note_file", None)
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    return getattr(args, "note", None)


def _finish(ctx: Ctx, report: Report, status_table: bool = False) -> int:
    for line in report.lines():
        ctx.say(line)
    if ctx.dry_run:
        ctx.say("dry run: nothing was changed")
    if status_table and not ctx.dry_run:
        ctx.say("")
        ctx.say(render_status(collect_status(ctx, integrations=False)))
    code = report.exit_code()
    if ctx.notify_enabled:
        outcome = {EXIT_OK: "done", EXIT_PARTIAL: "needs attention"}.get(code, "failed")
        ctx.notify("Fleet %s: %s" % (report.op, outcome), "\n".join(report.lines()) or "nothing to do",
                   "done" if code == EXIT_OK else "request")
    return code


def _status(ctx: Ctx, as_json: bool, hold: bool, stdin: TextIO) -> int:
    data = collect_status(ctx)
    ctx.say(json.dumps(data, indent=2) if as_json else render_status(data))
    if hold:
        wait_for_enter(ctx, stdin)
    return EXIT_OK


def _init(ctx: Ctx, args: argparse.Namespace) -> int:
    root, in_git, results = run_init(args.dir, args.example, args.force, ctx.env, ctx.cwd)
    ctx.say("root: %s (%s)" % (root, "Git work tree" if in_git else "not in a Git work tree; using the directory"))
    lines = ["%-11s %s%s" % (r.status, r.path, ": " + r.detail if r.detail else "") for r in results]
    for line in lines:
        ctx.say(line)
    failed = any(r.status == "failed" for r in results)
    if not failed:
        ctx.say("next: edit .herdr/fleet.toml, then run `herdr-fleet doctor` and `herdr-fleet up`")
    if ctx.notify_enabled:
        created = sum(r.status in ("created", "overwritten") for r in results)
        title = "Fleet init failed" if failed else "Fleet init: %d file(s) written in %s" % (created, root)
        ctx.notify(title, "\n".join(["root: %s" % root] + lines), "request" if failed else "done")
    return EXIT_FAIL if failed else EXIT_OK


def _doctor(ctx: Ctx) -> int:
    checks = run_doctor(ctx)
    for level, name, detail in checks:
        ctx.say("%-4s  %s: %s" % (level, name, detail))
    failed = any(level == "fail" for level, _, _ in checks)
    if ctx.notify_enabled:
        problems = ["%s %s: %s" % c for c in checks if c[0] != "ok"]
        title = "Fleet doctor: %s" % ("failed" if failed else "warnings" if problems else "all checks ok")
        ctx.notify(title, "\n".join(problems) or "all checks passed", "request" if problems else "done")
    return EXIT_FAIL if failed else EXIT_OK


def _fail(ctx: Ctx, args: argparse.Namespace, stdin: TextIO, code: int, kind: str, lines: List[str]) -> int:
    """Report an expected failure. Manifest actions show nothing on screen, so
    ``ui`` (always), ``--notify`` and any action invocation also notify; ``ui``
    exits 2 because its failures are discovery or precondition problems."""
    for line in lines:
        ctx.stderr.write("herdr-fleet: %s: %s\n" % (kind, line))
    if args.command == "ui":
        code = EXIT_USAGE
    if ctx.notify_enabled or args.command == "ui" or ctx.env.get("HERDR_PLUGIN_ACTION_ID"):
        what = args.entrypoint if args.command == "ui" else args.command
        ctx.notify("Fleet %s failed" % what, "\n".join(lines), "request")
    if getattr(args, "hold", False) or args.command == "menu":
        ctx.say("\n".join("error: %s" % line for line in lines))
        wait_for_enter(ctx, stdin)
    return code
