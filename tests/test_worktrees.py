"""worktrees (design 17.2) against real temporary git repositories built with
git init, git worktree add and git commit (``support.GitCase``).

Every git call the plugin makes goes through a logging shim on PATH, so the
tests also assert invariant 8: nothing fetches, prunes or writes."""

from __future__ import annotations

import json
import os
import shutil
import unittest

from support import GIT, GitCase
from herdr_fleet import gitinfo, worktrees

@unittest.skipUnless(GIT, "git is not installed")
class Worktrees(GitCase):
    def setUp(self):
        super().setUp()
        self.main_sha = self.git("rev-parse", "HEAD")
        self.set_origin(self.main_sha)
        self.wt = {name: os.path.join(self.real, name) for name in
                   ("wt-merged", "wt-new", "wt-detached", "wt-gone", "wt-locked")}
        self.git("worktree", "add", "-q", "-b", "slice/merged", self.wt["wt-merged"])
        self.git("worktree", "add", "-q", "-b", "slice/new", self.wt["wt-new"])
        self.commit("work in progress", cwd=self.wt["wt-new"], date="2026-01-01T00:00:00+00:00")
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(self.wt["wt-new"], name), "w") as handle:
                handle.write("dirty\n")
        self.git("worktree", "add", "-q", "--detach", self.wt["wt-detached"], "HEAD")
        self.git("worktree", "add", "-q", "-b", "slice/gone", self.wt["wt-gone"])
        shutil.rmtree(self.wt["wt-gone"])
        self.git("worktree", "add", "-q", "-b", "slice/locked", self.wt["wt-locked"])
        self.git("worktree", "lock", "--reason", "on a stick", self.wt["wt-locked"])
        self.write_fleet(self.read_fleet().replace(
            'name = "sample-dev"', 'name = "sample-dev"\nworktrees = ["%s/wt-new*", "%s/wt-detached"]'
            % (self.real, self.tmp)))  # the second glob uses the unresolved temp path

    def short(self, path):
        """The table shortens with ~ (HOME is the temp dir; git prints its real path)."""
        return "~" + path[len(self.real):] if path.startswith(self.real + "/") else path

    def test_table(self):
        with open(os.path.join(self.repo, ".git", "index"), "rb") as handle:
            index = handle.read()
        code, out, err = self.run_cli("worktrees")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "repo %s  default origin/main" % self.repo.replace(self.tmp, "~"))
        self.assertEqual(lines[1].split(), ["PATH", "BRANCH", "AGE", "STATE", "MERGED", "SEAT"])
        rows = {line.split()[0]: line for line in lines[2:]}
        self.assertEqual(len(rows), 6)
        self.assertEqual(sorted(rows), sorted(["~/repo"] + [self.short(p) for p in self.wt.values()]))
        rows = {os.path.join(self.real, k[2:]): v for k, v in rows.items()}
        main_row = rows[os.path.realpath(self.repo)]
        self.assertRegex(main_row, r"\bmain\s+0 min\s+1 changed\s+yes\s+main$")  # SEAT: main, unclaimed
        self.assertRegex(rows[self.wt["wt-merged"]], r"slice/merged\s+0 min\s+clean\s+yes$")
        self.assertRegex(rows[self.wt["wt-new"]], r"slice/new\s+\d+ w\s+2 changed\s+no\s+sample-dev$")
        self.assertRegex(rows[self.wt["wt-detached"]], r"detached @%s\s+0 min\s+clean\s+yes\s+sample-dev$"
                         % self.main_sha[:7])
        self.assertRegex(rows[self.wt["wt-gone"]], r"slice/gone\s+-\s+missing, prunable\s+yes$")
        self.assertRegex(rows[self.wt["wt-locked"]], r"slice/locked\s+0 min\s+clean, locked\s+yes$")
        self.assertEqual(self.calls(), [])  # no herdr command at all
        with open(os.path.join(self.repo, ".git", "index"), "rb") as handle:
            self.assertEqual(handle.read(), index)  # git status refreshed nothing
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".git", "worktrees", "wt-gone")))  # never pruned

    def git_order(self):
        return [line[len("worktree "):] for line in self.git("worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    def test_order_follows_git(self):
        out = self.run_cli("worktrees")[1]
        paths = [line.split()[0] for line in out.splitlines()[2:]]
        self.assertEqual(paths, [self.short(p) for p in self.git_order()])
        self.assertEqual(paths[0], "~/repo")
        self.assertEqual(sorted(paths[1:]), sorted(self.short(p) for p in self.wt.values()))

    def test_json(self):
        code, out, _ = self.run_cli("worktrees", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"repo", "default_branch", "worktrees"})
        self.assertEqual((data["repo"], data["default_branch"]), (self.repo, "origin/main"))
        by_path = {w["path"]: w for w in data["worktrees"]}
        keys = {"path", "branch", "head", "detached", "age_s", "changed", "missing", "prunable", "locked", "merged",
                "seats"}
        self.assertTrue(all(set(w) == keys for w in data["worktrees"]))
        new = by_path[self.wt["wt-new"]]
        self.assertEqual((new["branch"], new["detached"], new["changed"], new["merged"], new["seats"]),
                         ("slice/new", False, 2, False, ["sample-dev"]))
        self.assertGreater(new["age_s"], 7 * 86400)
        detached = by_path[self.wt["wt-detached"]]
        self.assertEqual((detached["branch"], detached["detached"], detached["head"]), (None, True, self.main_sha))
        gone = by_path[self.wt["wt-gone"]]
        self.assertEqual((gone["missing"], gone["prunable"], gone["changed"], gone["age_s"], gone["merged"]),
                         (True, True, None, None, True))
        self.assertTrue(by_path[self.wt["wt-locked"]]["locked"])
        self.assertEqual(by_path[os.path.realpath(self.repo)]["seats"], [])

    def test_seat_filter_and_unknown_seat(self):
        code, out, _ = self.run_cli("worktrees", "--seat", "sample-dev")
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in out.splitlines()[2:]],
                         [self.short(p) for p in self.git_order() if p in (self.wt["wt-new"], self.wt["wt-detached"])])
        out = self.run_cli("worktrees", "--seat", "sample-reviewer")[1]
        self.assertIn("sample-reviewer declares no worktrees globs", out)
        code, _, err = self.run_cli("worktrees", "--seat", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("not a seat: nobody", err)

    def test_status_hint_only_when_a_seat_declares_worktrees(self):
        self.fleet_ws()
        out = self.run_cli("status")[1]
        self.assertEqual(out.splitlines()[-1], "worktrees: see herdr-fleet worktrees")
        self.assertNotIn("wt-new", out)  # status lists no worktrees
        self.write_fleet(self.read_fleet().replace('worktrees = ["%s/wt-new*", "%s/wt-detached"]'
                                                   % (self.real, self.tmp), ""))
        self.assertNotIn("worktrees:", self.run_cli("status")[1])
        self.assertNotIn("worktrees:", json.dumps(json.loads(self.run_cli("status", "--json")[1])))


@unittest.skipUnless(GIT, "git is not installed")
class DefaultBranch(GitCase):
    def test_no_origin_at_all(self):
        code, out, _ = self.run_cli("worktrees")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].endswith("default -"))
        self.assertRegex(lines[2], r"\bclean\s+-\s+main$")
        self.assertIn("note: no default branch found", lines[-1])
        data = json.loads(self.run_cli("worktrees", "--json")[1])
        self.assertIsNone(data["default_branch"])
        self.assertIsNone(data["worktrees"][0]["merged"])

    def test_fallbacks_without_origin_head(self):
        sha = self.git("rev-parse", "HEAD")
        self.set_origin(sha, head=False, name="master")
        self.assertEqual(gitinfo.default_branch(self.repo, self.env), "origin/master")
        self.set_origin(sha, head=False, name="main")
        self.assertEqual(gitinfo.default_branch(self.repo, self.env), "origin/main")
        self.set_origin(sha, head=True, name="trunk")
        self.assertEqual(gitinfo.default_branch(self.repo, self.env), "origin/trunk")

    def test_not_a_git_repository(self):
        shutil.rmtree(os.path.join(self.repo, ".git"))
        code, _, err = self.run_cli("worktrees")
        self.assertEqual(code, 1)
        self.assertIn("not a git repository", err)


class Porcelain(unittest.TestCase):
    def test_parse(self):
        text = ("worktree /r\nHEAD 1111111111\nbranch refs/heads/main\n\n"
                "worktree /t/a\nHEAD 2222222222\ndetached\nlocked\n\n"
                "worktree /t/b\nHEAD 3333333333\nbranch refs/heads/x/y\nprunable gitdir file points to "
                "non-existent location\n\n"
                "worktree /bare\nbare\n")
        items = worktrees.parse_porcelain(text)
        self.assertEqual([(w.path, w.branch, w.detached, w.locked, w.prunable, w.bare) for w in items], [
            ("/r", "main", False, False, False, False), ("/t/a", None, True, True, False, False),
            ("/t/b", "x/y", False, False, True, False), ("/bare", None, False, False, False, True)])
        self.assertEqual(items[1].head, "2222222222")

    def test_globs(self):
        self.assertTrue(worktrees.matches(["/private/tmp/sample-*"], "/private/tmp/sample-export"))
        self.assertFalse(worktrees.matches(["/private/tmp/sample-*"], "/private/tmp/other"))
        self.assertFalse(worktrees.matches(["/private/tmp/Sample-*"], "/private/tmp/sample-x"))  # case matters
        self.assertFalse(worktrees.matches([], "/x"))


if __name__ == "__main__":
    unittest.main()
