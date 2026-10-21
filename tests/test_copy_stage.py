import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin"
RC_SYNC = 1792185002
STABLE_SYNC = 1792286001


class CopyStageTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "bin").mkdir()
        shutil.copy(BIN / "omarchy-mirror-copy-stage", self.root / "bin")
        shutil.copy(BIN / "omarchy-mirror-update-floor", self.root / "bin")
        rclone = self.root / "bin/rclone"
        rclone.write_text("#!/bin/bash\nexit 0\n")
        rclone.chmod(0o755)
        upload = self.root / "bin/omarchy-mirror-upload"
        upload.write_text('#!/bin/bash\necho "$*" >> "$(dirname "$0")/../uploads"\n')
        upload.chmod(0o755)

    def stage(self, name, lastsync, release):
        repo = self.root / name / "core/os/x86_64"
        repo.mkdir(parents=True)
        (repo / "core.db").write_text(f"{name} database\n")
        (repo / f"glibc-2.43-{release}-x86_64.pkg.tar.zst").touch()
        (self.root / name / "lastsync").write_text(f"{lastsync}\n")
        # Upstream's mtimes come along with every sync, so stages differ in them.
        for path in (self.root / name / "lastsync", repo / "core.db"):
            os.utime(path, (lastsync, lastsync))
        return self.root / name

    def copy(self, source, target):
        return subprocess.run(
            [str(self.root / "bin/omarchy-mirror-copy-stage"), "--from", f"{source}/", "--to", f"{target}/", "--r2", "omarchy:target"],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{self.root}/bin:{os.environ['PATH']}"},
        )

    def uploads(self):
        log = self.root / "uploads"
        return log.read_text().splitlines() if log.exists() else []

    def packages(self, stage):
        return sorted(path.name for path in (stage / "core/os/x86_64").glob("*.pkg.tar.zst"))

    def test_publishes_the_source_snapshot_as_it_is(self):
        stable = self.stage("stable", STABLE_SYNC, 2)
        rc = self.stage("rc", RC_SYNC, 1)

        result = self.copy(stable, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((rc / "lastsync").read_text(), f"{STABLE_SYNC}\n")
        self.assertEqual((rc / "core/os/x86_64/core.db").read_text(), "stable database\n")
        self.assertEqual(self.packages(rc), ["glibc-2.43-2-x86_64.pkg.tar.zst"])
        self.assertEqual(self.uploads(), [f"--stage {rc}/ --r2 omarchy:target"])

    def test_copies_the_same_snapshot_again(self):
        rc = self.stage("rc", RC_SYNC, 1)
        stable = self.stage("stable", RC_SYNC, 1)

        result = self.copy(rc, stable)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uploads(), [f"--stage {stable}/ --r2 omarchy:target"])

    def test_never_moves_a_mirror_back_to_an_older_snapshot(self):
        rc = self.stage("rc", RC_SYNC, 1)
        stable = self.stage("stable", STABLE_SYNC, 2)

        result = self.copy(rc, stable)

        self.assertEqual(result.returncode, 1)
        self.assertIn(f"newer snapshot ({STABLE_SYNC})", result.stderr)
        self.assertEqual((stable / "lastsync").read_text(), f"{STABLE_SYNC}\n")
        self.assertEqual(self.packages(stable), ["glibc-2.43-2-x86_64.pkg.tar.zst"])
        self.assertEqual(self.uploads(), [])

    def test_refuses_a_source_without_a_snapshot(self):
        empty = self.root / "empty"
        empty.mkdir()
        rc = self.stage("rc", RC_SYNC, 1)

        result = self.copy(empty, rc)

        self.assertEqual(result.returncode, 1)
        self.assertIn("no complete Arch snapshot", result.stderr)
        self.assertEqual(self.packages(rc), ["glibc-2.43-1-x86_64.pkg.tar.zst"])
        self.assertEqual(self.uploads(), [])

    def test_refuses_a_source_without_databases(self):
        stable = self.stage("stable", STABLE_SYNC, 2)
        (stable / "core/os/x86_64/core.db").unlink()
        rc = self.stage("rc", RC_SYNC, 1)

        result = self.copy(stable, rc)

        self.assertEqual(result.returncode, 1)
        self.assertEqual((rc / "lastsync").read_text(), f"{RC_SYNC}\n")
        self.assertEqual(self.uploads(), [])

    def test_refuses_a_source_missing_a_repository_the_target_serves(self):
        stable = self.stage("stable", STABLE_SYNC, 2)
        rc = self.stage("rc", RC_SYNC, 1)
        (rc / "extra/os/x86_64").mkdir(parents=True)
        (rc / "extra/os/x86_64/extra.db").write_text("rc extra database\n")

        result = self.copy(stable, rc)

        self.assertEqual(result.returncode, 1)
        self.assertIn("extra/os/x86_64/extra.db", result.stderr)
        self.assertTrue((rc / "extra/os/x86_64/extra.db").exists())
        self.assertEqual(self.uploads(), [])

    def test_refuses_a_target_whose_snapshot_cannot_be_read(self):
        rc = self.stage("rc", RC_SYNC, 1)
        stable = self.stage("stable", STABLE_SYNC, 2)
        (stable / "lastsync").unlink()

        result = self.copy(rc, stable)

        self.assertEqual(result.returncode, 1)
        self.assertIn("no readable lastsync", result.stderr)
        self.assertEqual(self.packages(stable), ["glibc-2.43-2-x86_64.pkg.tar.zst"])
        self.assertEqual(self.uploads(), [])

    def test_fills_an_empty_target(self):
        stable = self.stage("stable", STABLE_SYNC, 2)
        rc = self.root / "rc"
        rc.mkdir()

        result = self.copy(stable, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((rc / "lastsync").read_text(), f"{STABLE_SYNC}\n")

    def test_the_release_day_copy_goes_from_rc_to_stable(self):
        stable_from_rc = (BIN / "omarchy-mirror-sync-stable-from-rc").read_text()

        self.assertIn("--from /mnt/arch-mirror-rc/ --to /mnt/arch-mirror-stable/ --r2 omarchy:omarchy-mirror-stable", stable_from_rc)

    def test_nothing_stages_stable_from_upstream(self):
        # Stable only ever takes rc's snapshot; a sync from upstream is how it
        # once got ahead of rc.
        for script in BIN.iterdir():
            if script.is_file():
                text = script.read_text(errors="ignore")
                self.assertFalse(
                    "omarchy-mirror-sync" in text and '--stage "/mnt/arch-mirror-stable/"' in text,
                    f"{script.name} stages stable from upstream",
                )


if __name__ == "__main__":
    unittest.main()
