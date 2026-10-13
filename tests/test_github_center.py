"""Guards for the GitHub-centered workflow.

The runtime checkout is the only tree that may pull and restart processes.
A feature branch must not be published by pushing it onto main.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy-from-github.sh"
OPEN_PR = ROOT / "scripts" / "open-pr.sh"
AUTO_BACKUP = ROOT / "scripts" / "auto-backup.sh"

#: The three helper scripts start with ``#!/bin/zsh``. macOS has zsh at
#: ``/bin/zsh``; a Linux runner has to install it (.github/workflows/ci.yml
#: does). A machine without zsh skips these cases instead of erroring, because
#: "this box has no zsh" is not a defect in the tree.
ZSH = shutil.which("zsh")
needs_zsh = unittest.skipUnless(ZSH, "zsh is not installed")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c", "user.email=github-center@example.com",
            "-c", "user.name=github-center",
            "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_pair(root: Path) -> tuple[Path, Path]:
    bare = root / "bare.git"
    local = root / "local"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")
    _git(root, "clone", str(bare), str(local))
    (local / ".gitignore").write_text("/data/\n/data-v2/\n", encoding="utf-8")
    (local / "README").write_text("one\n", encoding="utf-8")
    _git(local, "add", ".gitignore", "README")
    _git(local, "commit", "-m", "one")
    _git(local, "push", "origin", "HEAD:refs/heads/main")
    return bare, local


def _run_deploy(local: Path, **env: str) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["ARB_DEPLOY_ROOT"] = str(local)
    merged["ARB_DEPLOY_SKIP_UV"] = "1"
    merged.update(env)
    return subprocess.run(
        [ZSH, str(DEPLOY)],
        cwd=local,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


class PublishRulesTest(unittest.TestCase):
    def test_backup_does_not_push_the_branch_onto_main(self) -> None:
        text = AUTO_BACKUP.read_text(encoding="utf-8")
        self.assertNotIn("HEAD:main", text)
        self.assertIn("open-pr.sh", text)

    @needs_zsh
    def test_open_pr_skips_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            result = subprocess.run(
                [ZSH, str(OPEN_PR)],
                cwd=local,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("open-pr: skip main", result.stdout)


@needs_zsh
class DeployGuardTest(unittest.TestCase):
    def test_refuses_without_runner_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            before = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            result = _run_deploy(local)
            after = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refusing", result.stdout)
        self.assertEqual(before, after)

    def test_dirty_tree_is_not_pulled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            (local / "data").mkdir()
            (local / "data" / "RUNTIME_ROLE").write_text("runner\n", encoding="utf-8")
            (local / "README").write_text("dirty\n", encoding="utf-8")
            before = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            result = _run_deploy(local)
            after = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, after)
            self.assertEqual((local / "README").read_text(encoding="utf-8"), "dirty\n")
            log = (local / "data" / "deploy-from-github.log").read_text(encoding="utf-8")
            self.assertIn("working tree dirty", log)

    def test_fast_forward_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            other = root / "other"
            _git(root, "clone", str(root / "bare.git"), str(other))
            (other / "README").write_text("two\n", encoding="utf-8")
            _git(other, "add", "README")
            _git(other, "commit", "-m", "two")
            _git(other, "push", "origin", "HEAD:refs/heads/main")
            (local / "data").mkdir()
            (local / "data" / "RUNTIME_ROLE").write_text("runner\n", encoding="utf-8")
            before = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            result = _run_deploy(local)
            after = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(before, after)
            self.assertEqual(
                after,
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=other, text=True
                ).strip(),
            )

    def test_diverged_main_is_not_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            other = root / "other"
            _git(root, "clone", str(root / "bare.git"), str(other))
            (other / "README").write_text("remote\n", encoding="utf-8")
            _git(other, "add", "README")
            _git(other, "commit", "-m", "remote")
            _git(other, "push", "origin", "HEAD:refs/heads/main")
            (local / "LOCAL").write_text("local\n", encoding="utf-8")
            _git(local, "add", "LOCAL")
            _git(local, "commit", "-m", "local")
            (local / "data").mkdir()
            (local / "data" / "RUNTIME_ROLE").write_text("runner\n", encoding="utf-8")
            before = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            result = _run_deploy(local)
            after = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, after)
            self.assertTrue((local / "LOCAL").exists())
            log = (local / "data" / "deploy-from-github.log").read_text(encoding="utf-8")
            self.assertIn("not a fast-forward", log)

    def test_cycle_lock_defers_live_and_still_restarts_the_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            (local / "data").mkdir()
            (local / "data-v2").mkdir()
            (local / "data" / "RUNTIME_ROLE").write_text("runner\n", encoding="utf-8")
            (local / "data-v2" / "xstocks_v2_cycle.lock").write_text("999999\n", encoding="utf-8")
            restart_log = local / "restarts.txt"
            # No synced sha: the tree is current with origin, but apply is owed.
            result = _run_deploy(
                local,
                ARB_DEPLOY_SERVICES="board,live",
                ARB_DEPLOY_RESTART_LOG=str(restart_log),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            restarted = restart_log.read_text(encoding="utf-8").split()
            self.assertEqual(restarted, ["board"])
            self.assertTrue((local / "data" / "deploy-live-pending").exists())

    def test_pending_live_restarts_once_the_lock_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            (local / "data").mkdir()
            (local / "data" / "RUNTIME_ROLE").write_text("runner\n", encoding="utf-8")
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            (local / "data" / "deploy-synced-sha").write_text(head + "\n", encoding="utf-8")
            (local / "data" / "deploy-live-pending").write_text("1\n", encoding="utf-8")
            restart_log = local / "restarts.txt"
            result = _run_deploy(
                local,
                ARB_DEPLOY_SERVICES="board",
                ARB_DEPLOY_RESTART_LOG=str(restart_log),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            restarted = restart_log.read_text(encoding="utf-8").split()
            # Only live: the tree was already at this commit, so nothing else
            # is owed a restart (the board is not part of a live changeover).
            self.assertEqual(restarted, ["live"])
            self.assertFalse((local / "data" / "deploy-live-pending").exists())

    def test_a_deferred_live_restart_does_not_restart_anything_else(self) -> None:
        """The tree is already current; only the live changeover is owed.

        Measured 2026-10-10: one MSTRx cycle held the lock and wrote HALT, so
        data/deploy-live-pending stayed on disk -- and every five-minute tick
        killed and started the board, the paper sampler, the watch and the
        discovery loop again. Re-applying an unchanged commit is churn, and it
        is the board the operator reads that blinks.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            (local / "data").mkdir()
            (local / "data-v2").mkdir()
            (local / "data" / "RUNTIME_ROLE").write_text("runner\n", encoding="utf-8")
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            (local / "data" / "deploy-synced-sha").write_text(head + "\n", encoding="utf-8")
            (local / "data" / "deploy-live-pending").write_text("1\n", encoding="utf-8")
            (local / "data-v2" / "xstocks_v2_cycle.lock").write_text("999999\n", encoding="utf-8")
            restart_log = local / "restarts.txt"
            result = _run_deploy(
                local,
                ARB_DEPLOY_SERVICES="board,live",
                ARB_DEPLOY_RESTART_LOG=str(restart_log),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            restarted = (restart_log.read_text(encoding="utf-8").split()
                         if restart_log.exists() else [])
            self.assertEqual(restarted, [])
            self.assertIn("live restart deferred", result.stdout)
            # The live restart is still owed, so the marker has to survive.
            self.assertTrue((local / "data" / "deploy-live-pending").exists())

    def test_a_pending_live_restart_still_changes_live_over(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, local = _init_pair(root)
            (local / "data").mkdir()
            (local / "data" / "RUNTIME_ROLE").write_text("runner\n", encoding="utf-8")
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=local, text=True
            ).strip()
            (local / "data" / "deploy-synced-sha").write_text(head + "\n", encoding="utf-8")
            (local / "data" / "deploy-live-pending").write_text("1\n", encoding="utf-8")
            restart_log = local / "restarts.txt"
            result = _run_deploy(
                local,
                ARB_DEPLOY_SERVICES="board,paper,live",
                ARB_DEPLOY_RESTART_LOG=str(restart_log),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            # Only live, and it is no longer owed afterwards.
            self.assertEqual(restart_log.read_text(encoding="utf-8").split(), ["live"])
            self.assertFalse((local / "data" / "deploy-live-pending").exists())


if __name__ == "__main__":
    unittest.main()
