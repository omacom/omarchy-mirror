import fcntl
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin"


class StageTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.log = self.root / "rsync.log"
        (self.root / "bin").mkdir()

    def stage_with_rsync(self, code):
        # rsync stand-in: record each argument, copy the upstream's lastsync
        # into the stage as rsync would unless it is left out, and exit with
        # the given code.
        rsync = self.root / "bin/rsync"
        rsync.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$@" > "{self.log}"\n'
            'stage="${@: -1}"\n'
            'grep -qx -- "--exclude=/lastsync" "' + str(self.log) + '" || echo 1999999999 > "${stage}lastsync"\n'
            f"exit {code}\n"
        )
        rsync.chmod(0o755)
        return subprocess.run(
            [str(BIN / "omarchy-mirror-stage"), "--upstream", "mirror.example/archlinux/", "--stage", f"{self.stage}/"],
            capture_output=True,
            text=True,
            env={"PATH": f"{self.root}/bin:/usr/bin:/bin"},
        )

    def args(self):
        return self.log.read_text().splitlines()

    def test_a_complete_sync_is_stamped_by_the_stage(self):
        result = self.stage_with_rsync(0)

        self.assertEqual(result.returncode, 0, result.stderr)
        stamp = (self.stage / "lastsync").read_text().strip()
        self.assertTrue(stamp.isdigit() and stamp != "1999999999", stamp)

    def test_a_failed_sync_leaves_no_stamp(self):
        # 23 is a partial transfer, 24 vanished files, 30 a timeout: in each
        # the stage may not hold what its databases name. The stamp of the
        # last complete run goes too, since the stage no longer holds it.
        for code in (23, 24, 30):
            with self.subTest(code=code):
                (self.stage / "lastsync").write_text("1700000000\n")
                result = self.stage_with_rsync(code)

                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(self.log.exists(), "rsync ran")
                self.assertFalse((self.stage / "lastsync").exists())

    def test_the_upstreams_temp_files_are_hidden(self):
        # The upstream writes .<name>.<random> while it syncs, readable only
        # by itself: rsync failed on them in 23 of 720 runs in November 2026.
        # A sender-side hide, unlike an exclude, leaves local dot files to
        # --delete-delay.
        self.stage_with_rsync(0)

        self.assertIn("--filter=hide .*", self.args())
        self.assertFalse(any(a.startswith("--exclude=.") for a in self.args()), self.args())


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        shutil.copy(BIN / "omarchy-mirror-sync", self.bin)
        self.log = self.root / "steps.log"

    def sync(self, stage_code, upload_code):
        # Stage and upload stand-ins next to the real sync script.
        for name, code in (("omarchy-mirror-stage", stage_code), ("omarchy-mirror-upload", upload_code)):
            step = self.bin / name
            step.write_text(f'#!/bin/bash\necho {name} >> "{self.log}"\nexit {code}\n')
            step.chmod(0o755)
        return subprocess.run(
            [str(self.bin / "omarchy-mirror-sync"), "--upstream", "u", "--stage", "s/", "--r2", "r"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
            cwd=self.root,
        )

    def steps(self):
        return self.log.read_text().split() if self.log.exists() else []

    def test_runs_both_steps(self):
        result = self.sync(0, 0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ["omarchy-mirror-stage", "omarchy-mirror-upload"])

    def test_a_failed_stage_uploads_nothing(self):
        result = self.sync(1, 0)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.steps(), ["omarchy-mirror-stage"])

    def test_a_stage_in_use_is_left_alone(self):
        # Another sync or copy holds the stage's lock: this run touches
        # nothing, so it can neither stamp nor upload that run's tree.
        with open(self.root / "s.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.sync(0, 0)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("in use", result.stderr)
        self.assertEqual(self.steps(), [])

    def test_a_failed_upload_fails_the_sync(self):
        result = self.sync(0, 1)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.steps(), ["omarchy-mirror-stage", "omarchy-mirror-upload"])
        self.assertNotIn("Finished syncing", result.stdout)


@unittest.skipUnless(shutil.which("rsync"), "needs rsync")
class StageWithRsyncTest(unittest.TestCase):
    """The stage against a real rsync daemon serving a small upstream."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.up, self.stage = self.root / "up", self.root / "stage"
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        conf = self.root / "rsyncd.conf"
        conf.write_text(
            f"use chroot = no\nlog file = {self.root}/rsyncd.log\npid file = {self.root}/rsyncd.pid\n"
            f"[archlinux]\npath = {self.up}\nread only = yes\n"
        )
        self.up.mkdir()
        daemon = subprocess.Popen(
            ["rsync", "--daemon", "--no-detach", f"--config={conf}", f"--port={self.port}", "--address=127.0.0.1"]
        )
        self.addCleanup(daemon.wait)
        self.addCleanup(daemon.terminate)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)

    def write(self, base, rel, text="x"):
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_stages_the_mirror_without_dot_files_or_the_upstreams_stamp(self):
        repo = "core/os/x86_64"
        self.write(self.up, f"{repo}/core.db", "new database")
        self.write(self.up, f"{repo}/glibc-2.43-1-x86_64.pkg.tar.zst")
        self.write(self.up, f"{repo}/.core.db.tar.gz.Ab12Cd")  # the upstream mid-sync
        self.write(self.up, "lastsync", "1999999999\n")
        # An interrupted run left rsync scratch files: one under a directory
        # the upstream still serves, one under a directory it dropped.
        self.write(self.stage, f"{repo}/.~tmp~/glibc-2.42-9-x86_64.pkg.tar.zst")
        self.write(self.stage, "obsolete/.~tmp~/old-1-1-x86_64.pkg.tar.zst")
        self.write(self.stage, "lastsync", "1700000000\n")

        result = subprocess.run(
            [str(BIN / "omarchy-mirror-stage"), "--upstream", f"127.0.0.1:{self.port}/archlinux/",
             "--stage", f"{self.stage}/"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        staged = sorted(str(p.relative_to(self.stage)) for p in self.stage.rglob("*"))
        self.assertEqual(staged, ["core", "core/os", f"{repo}", f"{repo}/core.db",
                                  f"{repo}/glibc-2.43-1-x86_64.pkg.tar.zst", "lastsync"])
        stamp = (self.stage / "lastsync").read_text().strip()
        self.assertNotIn(stamp, ("1999999999", "1700000000"))


if __name__ == "__main__":
    unittest.main()
