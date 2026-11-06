"""Design section 39 (0.19.4): worktree discovery, Kimi's box, prompt-aware limit reads,
allowance-aware fallback order and current failover reasons. All fixtures are synthetic.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import fake_herdr
from support import CALLER_PANE, FleetCase, LiveFleet
from herdr_fleet import context, context_size, crossfleet, fleetfile, inbox, limits, messages, ops, presence, watch
from herdr_fleet.inbox import NEW
from test_inbox import box, put
from test_section35 import CLAUDE_LIMIT, KIMI_LIMIT, TIERS, TierLive
from test_watch import StopClock, WatchCase, events


FLEET = '''workspace = "sample"

[[agents]]
name = "sample-orchestrator"
kind = "claude"

[[agents]]
name = "sample-dev"
kind = "claude"

[[agents]]
name = "sample-reviewer"
kind = "codex"
'''


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", repo] + list(args), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, encoding="utf-8", env=env)


def old_time(minutes=10):
    return inbox.iso_local(inbox.now_utc() - datetime.timedelta(minutes=minutes))


def make_kimi(case):
    case.write_fleet(case.read_fleet().replace(
        'name = "sample-dev"\nkind = "claude"\nargs = ["--model", "claude-opus-5-5"]',
        'name = "sample-dev"\nkind = "kimi"\nargs = ["-m", "kimi-code/k3.1-pro", "-y"]'))
    fake_herdr.put_agent(case.world, "w2:p2", "kimi", "sample-dev", args=["-m", "kimi-code/k3.1-pro", "-y"])
    state = case.read_state()
    state["agents"]["sample-dev"].update(kind="kimi", args=["-m", "kimi-code/k3.1-pro", "-y"])
    case.write_state(state)


class WorktreeFleet(FleetCase):
    fleet_text = FLEET

    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(self.tmp)
        self.repo = os.path.realpath(self.repo)
        self.git_env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "Sample Builder", "GIT_AUTHOR_EMAIL": "sample@example.test",
                        "GIT_COMMITTER_NAME": "Sample Builder", "GIT_COMMITTER_EMAIL": "sample@example.test",
                        "LANG": "C"}
        subprocess.run(["git", "init", self.repo], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=self.git_env)
        git(self.repo, "config", "user.name", "Sample Builder", env=self.git_env)
        git(self.repo, "config", "user.email", "sample@example.test", env=self.git_env)
        git(self.repo, "add", ".herdr/fleet.toml", env=self.git_env)
        git(self.repo, "commit", "-m", "sample fleet", env=self.git_env)
        self.linked = os.path.join(self.tmp, "linked")
        git(self.repo, "worktree", "add", "-b", "sample-linked", self.linked, env=self.git_env)

    def test_walkup_discovery_uses_the_main_checkout(self):
        found = fleetfile.find_fleet_file(self.linked)
        self.assertEqual(found, os.path.join(self.repo, ".herdr", "fleet.toml"))

    def test_explicit_fleet_file_and_environment_keep_the_worktree(self):
        own = os.path.join(self.linked, ".herdr", "fleet.toml")
        for explicit, env in ((own, {}), (own, {}), (None, {"HERDR_FLEET_FILE": own})):
            with self.subTest(explicit=explicit, env=env):
                self.assertEqual(fleetfile.find_fleet_file(self.linked, explicit, env), own)

    def test_a_brought_up_worktree_keeps_its_own_fleet(self):
        with open(os.path.join(self.linked, ".herdr", "fleet.state.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.assertEqual(fleetfile.find_fleet_file(self.linked), os.path.join(self.linked, ".herdr", "fleet.toml"))

    def test_a_main_checkout_without_a_fleet_keeps_the_worktree(self):
        main = os.path.join(self.repo, ".herdr", "fleet.toml")
        os.rename(main, main + ".away")
        self.assertEqual(fleetfile.find_fleet_file(self.linked), os.path.join(self.linked, ".herdr", "fleet.toml"))

    def test_no_commondir_and_an_unresolved_pointer_keep_the_worktree(self):
        pointer = os.path.join(self.linked, ".git")
        with open(pointer, encoding="utf-8") as handle:
            original = handle.read()
        gitdir = original.strip().split(":", 1)[1].strip()
        os.makedirs(os.path.join(self.tmp, "no-common"))
        try:
            with open(pointer, "w", encoding="utf-8") as handle:
                handle.write("gitdir: %s\n" % os.path.join(self.tmp, "no-common"))
            self.assertEqual(fleetfile.find_fleet_file(self.linked),
                             os.path.join(self.linked, ".herdr", "fleet.toml"))
            with open(pointer, "w", encoding="utf-8") as handle:
                handle.write("gitdir: %s\n" % os.path.join(self.tmp, "missing"))
            self.assertEqual(fleetfile.find_fleet_file(self.linked),
                             os.path.join(self.linked, ".herdr", "fleet.toml"))
        finally:
            with open(pointer, "w", encoding="utf-8") as handle:
                handle.write(original)
        self.assertTrue(os.path.isfile(os.path.join(gitdir, "commondir")))

    def test_send_from_a_worktree_queues_in_the_main_mailbox_and_notes_once(self):
        code, out, err = self.run_cli("send", "sample-dev", "Review the worktree", "--as",
                                      "sample-orchestrator", cwd=self.linked)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 1)
        self.assertEqual(box(self.linked, "sample-dev", NEW), [])
        self.assertEqual(err.count("is a linked worktree"), 1, err)
        self.assertIn("using the main checkout's fleet", err)
        self.assertIn("discovered from a linked worktree", out)

    def test_timers_discovery_notes_the_switch_once(self):
        code, out, err = self.run_cli("timers", "--dry-run", cwd=self.linked)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err.count("is a linked worktree"), 1, err)
        self.assertIn("using the main checkout's fleet", err)

    def test_crossfleet_destination_and_local_root_use_the_main_checkout(self):
        agent = {"name": "sample-dev", "cwd": self.linked}
        self.assertEqual(crossfleet.fleet_root(self.linked), self.repo)
        source = os.path.join(self.tmp, "source")
        os.makedirs(os.path.join(source, ".herdr"))
        with open(os.path.join(source, ".herdr", "fleet.toml"), "w", encoding="utf-8") as handle:
            handle.write(FLEET)
        fleet, name, reason = crossfleet.destination(agent, [agent], source)
        self.assertEqual((fleet.repo, name, reason), (self.repo, "sample-dev", None))
        self.assertIsNone(crossfleet.other_fleet(agent, self.repo))

    def test_fleet_flag_from_a_worktree_is_empty(self):
        ctx = context.Ctx(None, self.env, self.clock, io.StringIO(), io.StringIO(), self.linked)
        self.assertEqual(messages.fleet_flag(ctx), "")

    def test_hook_from_a_worktree_is_silent(self):
        self.world["panes"][CALLER_PANE]["agent"]["name"] = "sample-orchestrator"
        code, out, err = self.run_cli("inbox", "--hook", "stop", stdin=json.dumps({"cwd": self.linked}),
                                      cwd=self.linked)
        self.assertEqual((code, out, err), (0, "", ""))

    def test_doctor_warns_about_stranded_mail(self):
        put(self.linked, "sample-dev", "One")
        put(self.linked, "sample-reviewer", "Two")
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0, out + err)
        self.assertIn("warn  stranded mail: 2 queued in %s (sample-dev 1, sample-reviewer 1)" % self.linked, out)
        self.assertIn("nothing delivers them: herdr-fleet inbox --fleet", out)

    def test_doctor_ignores_empty_and_brought_up_worktrees(self):
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("stranded mail", out)
        put(self.linked, "sample-dev", "Owned here")
        with open(os.path.join(self.linked, ".herdr", "fleet.state.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("stranded mail", out)


class KimiPrompt(unittest.TestCase):
    def test_empty_box_and_quoted_prompt_line(self):
        screen = "A quoted answer follows.\n> this is not the input\n" + fake_herdr.prompt_screen("kimi")
        self.assertEqual(presence.prompt_line(screen, "kimi"), "")
        self.assertFalse(presence.is_typed(presence.prompt_line(screen, "kimi"), "kimi"))

    def test_text_and_the_idle_cache_menu_hold(self):
        self.assertEqual(presence.prompt_line(fake_herdr.prompt_screen("kimi", "half typed"), "kimi"), "half typed")
        self.assertIsNone(presence.prompt_line(fake_herdr.KIMI_IDLE_MENU, "kimi"))
        self.assertTrue(presence.is_typed(None, "kimi"))


class KimiSend(LiveFleet):
    def setUp(self):
        super().setUp()
        make_kimi(self)

    def test_empty_box_delivers(self):
        code, out, err = self.run_cli("send", "sample-dev", "Build the section", "--as", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_text_in_the_box_queues(self):
        fake_herdr.type_in(self.world, "w2:p2", "half typed")
        code, out, err = self.run_cli("send", "sample-dev", "Build the section", "--as", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertIn("text in its input (w2:p2)", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_idle_cache_menu_queues_as_a_dialog(self):
        fake_herdr.show_dialog(self.world, "w2:p2")
        code, out, err = self.run_cli("send", "sample-dev", "Build the section", "--as", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertIn("a dialog or a menu is on screen in w2:p2", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 1)


class KimiInterrupt(LiveFleet):
    def setUp(self):
        super().setUp()
        make_kimi(self)
        self.status_of("w2:p2", "working")
        self.write_fleet(self.read_fleet() + '\n[interrupt]\nkimi = ["ctrl+c"]\n')

    def test_restored_prompt_is_held_without_ctrl_u(self):
        self.world["behaviors"]["restore_prompt_on_interrupt"] = True
        code, out, err = self.run_cli("interrupt", "sample-dev", "--as", "sample-orchestrator")
        self.assertEqual(code, 3, out + err)
        self.assertIn("its input still holds text; press Ctrl-U there before sending it anything", out)
        self.assertEqual(self.argvs("agent", "send-keys"), [["agent", "send-keys", "sample-dev", "ctrl+c"]])

    def test_no_restored_prompt_succeeds_without_ctrl_u(self):
        code, out, err = self.run_cli("interrupt", "sample-dev", "--as", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("agent", "send-keys"), [["agent", "send-keys", "sample-dev", "ctrl+c"]])


class DialogClock(StopClock):
    def __init__(self, on_hold):
        super().__init__(cycles=1)
        self.on_hold, self.holds = on_hold, 0

    def sleep(self, seconds):
        if seconds == watch.HOLD_RECHECK_S:
            self.holds += 1
            if self.holds > 200:
                raise KeyboardInterrupt
            self.on_hold(self.holds)
        super().sleep(seconds)


class KimiWatcher(WatchCase):
    def setUp(self):
        super().setUp()
        make_kimi(self)
        put(self.repo, "sample-dev", "Task A")
        fake_herdr.show_dialog(self.world, "w2:p2")
        self.behave(wait_results={"sample-dev": ["idle"]})

    def edit_world(self, change):
        with open(self.world_path, encoding="utf-8") as handle:
            world = json.load(handle)
        change(world)
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(world, handle)

    def test_dialog_hold_notifies_once_then_delivers_when_the_box_returns(self):
        def change(number):
            if number == 102:
                self.edit_world(lambda world: fake_herdr.set_screen(world, "w2:p2", None))
        self.clock = DialogClock(change)
        code, out, err = self.watch(cycles=1)
        self.assertEqual(code, 0, out + err)
        titles = [n["title"] for n in self.world["notifications"]
                  if n["title"] == "sample-dev has a dialog or menu open (w2:p2): 1 queued wait"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        self.assertEqual(events(out).count(titles[0]), 1)

    def test_notify_blocked_after_overrides_the_dialog_delay(self):
        self.write_fleet(self.read_fleet() + '\n[watch]\nnotify_blocked_after = "1m"\n')
        def change(number):
            if number == 22:
                self.edit_world(lambda world: fake_herdr.set_screen(world, "w2:p2", None))
        self.clock = DialogClock(change)
        code, out, err = self.watch(cycles=1)
        self.assertEqual(code, 0, out + err)
        titles = [n["title"] for n in self.world["notifications"] if "has a dialog or menu open" in n["title"]]
        self.assertEqual(len(titles), 1)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_typed_text_hold_never_notifies(self):
        fake_herdr.type_in(self.world, "w2:p2", "half typed")
        def change(number):
            if number == 102:
                self.edit_world(lambda world: fake_herdr.type_in(world, "w2:p2", ""))
        self.clock = DialogClock(change)
        code, out, err = self.watch(cycles=1)
        self.assertEqual(code, 0, out + err)
        self.assertEqual([n for n in self.world["notifications"] if "dialog or menu" in n["title"]], [])
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)


class LogClock(StopClock):
    def __init__(self, at_pass, entry):
        super().__init__()
        self.at_pass, self.entry = at_pass, entry

    def sleep(self, seconds):
        if seconds == watch.CYCLE_S and self.passes + 1 == self.at_pass:
            messages.append(self.entry.pop("repo"), self.entry)
        super().sleep(seconds)


class LimitReads(WatchCase, TierLive):
    def quiet(self, name="sample-dev"):
        state = self.read_state()
        state["agents"][name]["state_seen"] = {"state": "idle", "since": old_time()}
        self.write_state(state)

    def prompt_case(self, kind):
        entry = {"time": old_time(), "from": "sample-orchestrator", "to": "sample-dev", "kind": kind}
        if kind == "send":
            entry["delivery"] = "sent"
        messages.append(self.repo, entry)
        self.quiet()
        later = dict(entry, time=old_time(9))
        self.clock = LogClock(int(watch.LIMIT_SCAN_S / watch.CYCLE_S) + 5, dict(later, repo=self.repo))
        code, out, err = self.watch(cycles=3 * (int(watch.LIMIT_SCAN_S / watch.CYCLE_S) + 3))
        self.assertEqual(code, 0, out + err)
        reads = [a for a in self.argvs("agent", "read", "sample-dev")]
        self.assertEqual(len(reads), 2, reads)

    def test_delivered_prompt_causes_one_more_read(self):
        self.prompt_case("delivered")

    def test_sent_prompt_causes_one_more_read(self):
        self.prompt_case("send")

    def test_kickoff_prompt_causes_one_more_read(self):
        self.prompt_case("kickoff")

    def test_unchanged_log_is_not_reread(self):
        entry = {"time": old_time(), "from": "sample-orchestrator", "to": "sample-dev", "kind": "delivered"}
        messages.append(self.repo, entry)
        watcher = object.__new__(watch.Watcher)
        watcher.repo = self.repo
        watcher.prompt_log_offset = 0
        watcher.prompt_log_signature = None
        watcher.last_prompt = {}
        with mock.patch("builtins.open", wraps=open) as opened:
            watcher.refresh_last_prompts()
            watcher.refresh_last_prompts()
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(watcher.last_prompt["sample-dev"], entry["time"])

    def test_replaced_log_is_read_from_the_start(self):
        messages.append(self.repo, {"time": old_time(), "from": "sample-orchestrator", "to": "sample-dev",
                                    "kind": "delivered"})
        watcher = object.__new__(watch.Watcher)
        watcher.repo = self.repo
        watcher.prompt_log_offset = 0
        watcher.prompt_log_signature = None
        watcher.last_prompt = {}
        watcher.refresh_last_prompts()
        path = messages.log_path(self.repo)
        size = os.stat(path).st_size
        replacement = path + ".new"
        entry = {"time": old_time(9), "from": "sample-orchestrator", "to": "sample-reviewer", "kind": "kickoff"}
        raw = json.dumps(entry)
        with open(replacement, "w", encoding="utf-8") as handle:
            handle.write(raw + " " * max(1, size - len(raw)) + "\n")
        os.replace(replacement, path)
        watcher.refresh_last_prompts()
        self.assertEqual(watcher.last_prompt["sample-reviewer"], entry["time"])


class Fallback(TierLive):
    def allowance(self, percent=97, reset=None, read_at=None):
        state = self.read_state()
        state["codex_allowance"] = {
            "used_percent": percent,
            "resets_at": int((reset or (inbox.now_utc() + datetime.timedelta(days=1))).timestamp()),
            "window_minutes": 10080,
            "read_at": read_at or inbox.iso_local(inbox.now_utc()),
        }
        self.write_state(state)

    def swap(self, *args, **kwargs):
        return self.run_cli("swap", *args, **kwargs)

    def test_fresh_high_reading_demotes_codex(self):
        self.allowance()
        code, out, err = self.swap("sample-orchestrator", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn("fallback - claude -> kimi (codex at 97% of the weekly allowance until", out)
        self.assertIn("tried last)", out)
        self.assertEqual(self.argvs("agent", "start")[0][3:5], ["--kind", "kimi"])

    def test_stale_state_uses_the_latest_rollout(self):
        self.allowance(read_at=old_time(20))
        root = os.path.join(self.tmp, "codex-sessions")
        day = inbox.now_utc().astimezone(datetime.timezone.utc).date()
        folder = os.path.join(root, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day)
        os.makedirs(folder)
        path = os.path.join(folder, "rollout-sample.jsonl")
        reset = int((inbox.now_utc() + datetime.timedelta(days=1)).timestamp())
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 98, "window_minutes": 10080, "resets_at": reset}}}}) + "\n")
        env = dict(self.env, HERDR_FLEET_CODEX_SESSIONS=root)
        code, out, err = self.swap("sample-orchestrator", "--fallback", env=env)
        self.assertEqual(code, 0, out + err)
        self.assertIn("codex at 98%", out)
        self.assertEqual(self.argvs("agent", "start")[0][3:5], ["--kind", "kimi"])

    def test_below_threshold_after_reset_and_no_reading_keep_the_order(self):
        for reading in ("below", "reset", "none"):
            with self.subTest(reading=reading):
                if reading != "none":
                    self.allowance(percent=94 if reading == "below" else 97,
                                   reset=inbox.now_utc() + datetime.timedelta(days=1) if reading == "below"
                                   else inbox.now_utc() - datetime.timedelta(minutes=1))
                else:
                    state = self.read_state()
                    state.pop("codex_allowance", None)
                    self.write_state(state)
                ctx = context.Ctx(None, self.env, self.clock, io.StringIO(), io.StringIO(), self.repo)
                plan = ops._fallback_plan(ctx, ctx.fleet.seat("sample-orchestrator"), None,
                                          self.read_state()["agents"], True, context.Report("swap"))
                self.assertEqual(plan[1], ["codex", "kimi"])

    def test_codex_as_the_only_candidate_is_still_taken_with_the_line(self):
        self.allowance()
        self.write_fleet(self.read_fleet().replace('prefer = ["claude", "kimi"]', 'prefer = ["claude", "codex"]')
                         .replace('extra_args = { kimi = ["--add-dir", "../shared"] }\n', ""))
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn("fallback - claude -> codex (codex at 97% of the weekly allowance until", out)
        self.assertNotIn("tried last", out)
        self.assertEqual(self.argvs("agent", "start")[0][3:5], ["--kind", "codex"])

    def test_dry_run_reads_but_writes_nothing(self):
        self.allowance()
        path = os.path.join(self.repo, ".herdr", "fleet.state.json")
        with open(path, "rb") as handle:
            before = handle.read()
        code, out, err = self.swap("sample-orchestrator", "--fallback", "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("tried last", out)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_passed_record_is_not_quoted_and_reason_is_the_command(self):
        record = limits.new_record("claude", CLAUDE_LIMIT, inbox.now_utc())
        record["resets_at"] = old_time(1)
        state = self.read_state()
        state["agents"]["sample-dev"]["limited"] = record
        self.write_state(state)
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(CLAUDE_LIMIT, self.prompts_to("sample-dev")[-1])
        self.assertIn("the owner asked for the fallback of claude", self.prompts_to("sample-dev")[-1])
        self.assertEqual(self.read_state()["agents"]["sample-dev"]["failover"]["reason"], "swap --fallback")

    def test_released_record_is_not_quoted(self):
        record = dict(limits.new_record("claude", CLAUDE_LIMIT, inbox.now_utc()), released=True)
        state = self.read_state()
        state["agents"]["sample-dev"]["limited"] = record
        self.write_state(state)
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(CLAUDE_LIMIT, self.prompts_to("sample-dev")[-1])
        self.assertEqual(self.read_state()["agents"]["sample-dev"]["failover"]["reason"], "swap --fallback")

    def test_counting_record_is_quoted(self):
        state = self.read_state()
        state["agents"]["sample-dev"]["limited"] = limits.new_record("claude", CLAUDE_LIMIT, inbox.now_utc())
        self.write_state(state)
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn(CLAUDE_LIMIT, self.prompts_to("sample-dev")[-1])
        self.assertEqual(self.read_state()["agents"]["sample-dev"]["failover"]["reason"], CLAUDE_LIMIT)

    def test_codex_reading_below_threshold_is_not_cited(self):
        self.allowance(percent=80)
        fake_herdr.put_agent(self.world, "w2:p2", "codex", "sample-dev", args=["-m", "gpt-6-astra"])
        state = self.read_state()
        state["agents"]["sample-dev"].update(kind="codex", args=["-m", "gpt-6-astra"])
        self.write_state(state)
        self.write_fleet(self.read_fleet().replace('prefer = ["claude", "kimi"]', 'prefer = ["codex", "claude"]')
                         .replace('extra_args = { kimi = ["--add-dir", "../shared"] }\n', ""))
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn("the owner asked for the fallback of codex", self.prompts_to("sample-dev")[-1])
        self.assertNotIn("80%", self.prompts_to("sample-dev")[-1])


class LatestAllowance(unittest.TestCase):
    def test_newest_rollout_in_the_newest_two_date_directories(self):
        root = tempfile.mkdtemp(prefix="sample-rollouts-")
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        today = inbox.now_utc().astimezone(datetime.timezone.utc).date()
        paths = []
        for days, percent in ((3, 99), (2, 80), (1, 91), (0, 92)):
            day = today - datetime.timedelta(days=days)
            folder = os.path.join(root, "%04d" % day.year, "%02d" % day.month, "%02d" % day.day)
            os.makedirs(folder)
            path = os.path.join(folder, "rollout-sample.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": percent, "window_minutes": 10080,
                                "resets_at": int((inbox.now_utc() + datetime.timedelta(days=1)).timestamp())}}}}) + "\n")
            paths.append(path)
        os.utime(paths[1], (100, 100))
        os.utime(paths[2], (300, 300))
        os.utime(paths[3], (200, 200))
        reading = context_size.latest_allowance({"HERDR_FLEET_CODEX_SESSIONS": root})
        self.assertEqual(reading["used_percent"], 91.0)


if __name__ == "__main__":
    unittest.main()
