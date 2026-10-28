import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin"
OCT_7 = 1791398870
OCT_20 = 1792494243


def rclone_env(root):
    # An omarchy: remote on the local filesystem, and no rclone.conf of the
    # machine's own.
    config = Path(root) / "rclone.conf"
    config.touch()
    return {**os.environ, "RCLONE_CONFIG": str(config), "RCLONE_CONFIG_OMARCHY_TYPE": "local"}


# The real rclone over local directories standing in for the buckets, so the
# filters are rclone's own: a stand-in would agree with whatever the scripts
# assume about them.
@unittest.skipUnless(shutil.which("rclone"), "needs rclone")
class PromoteArmTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.env = rclone_env(self.root)

    def bucket(self, name, lastsync=None, core_lastsync=None, release=1):
        path = self.root / name
        repo = path / "aarch64/core"
        repo.mkdir(parents=True)
        (repo / "core.db").write_text(f"{name} database {release}\n")
        (repo / f"glibc-2.43-{release}-aarch64.pkg.tar.xz").write_text(f"glibc {release}\n")
        if lastsync is not None:
            (path / "lastsync").write_text(f"{lastsync}\n")
        if core_lastsync is not None:
            (repo / "lastsync").write_text(f"{core_lastsync}\n")
        return path

    def remote(self, path):
        return f"omarchy:{path}"

    def promote(self, source, target):
        return subprocess.run(
            [str(BIN / "omarchy-mirror-promote-arm"), "--from", self.remote(source), "--to", self.remote(target)],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_promotes_a_newer_snapshot_with_its_stamps(self):
        edge = self.bucket("edge", OCT_20, OCT_20, release=2)
        rc = self.bucket("rc", OCT_7, OCT_7, release=1)

        result = self.promote(edge, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((rc / "lastsync").read_text(), f"{OCT_20}\n")
        self.assertEqual((rc / "aarch64/core/lastsync").read_text(), f"{OCT_20}\n")
        self.assertEqual((rc / "aarch64/core/core.db").read_text(), "edge database 2\n")
        self.assertEqual(sorted(p.name for p in (rc / "aarch64/core").glob("*.pkg.tar.xz")), ["glibc-2.43-2-aarch64.pkg.tar.xz"])

    def test_refuses_to_move_a_bucket_back(self):
        rc = self.bucket("rc", OCT_7, OCT_7, release=1)
        stable = self.bucket("stable", OCT_20, OCT_20, release=2)

        result = self.promote(rc, stable)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"serves a newer snapshot ({OCT_20}) than", result.stderr)
        self.assertEqual((stable / "aarch64/core/core.db").read_text(), "stable database 2\n")
        self.assertEqual((stable / "lastsync").read_text(), f"{OCT_20}\n")

    def test_reads_core_stamp_where_the_bucket_has_no_root_one(self):
        # The buckets promoted on Oct 7 carry only the repositories' stamps.
        edge = self.bucket("edge", core_lastsync=OCT_20, release=2)
        rc = self.bucket("rc", core_lastsync=OCT_7, release=1)
        self.assertEqual(self.promote(edge, rc).returncode, 0)

        older = self.bucket("older", core_lastsync=OCT_7, release=1)
        result = self.promote(older, rc)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("serves a newer snapshot", result.stderr)

    def test_refuses_a_source_without_a_stamp(self):
        source = self.bucket("source", release=2)
        rc = self.bucket("rc", OCT_7, OCT_7, release=1)

        result = self.promote(source, rc)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no readable lastsync", result.stderr)
        self.assertEqual((rc / "aarch64/core/core.db").read_text(), "rc database 1\n")

    def test_refuses_a_populated_destination_without_a_stamp(self):
        edge = self.bucket("edge", OCT_20, OCT_20, release=2)
        rc = self.bucket("rc", release=1)

        result = self.promote(edge, rc)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no readable lastsync to compare with", result.stderr)

    def test_refuses_a_destination_it_cannot_list(self):
        edge = self.bucket("edge", OCT_20, OCT_20, release=2)
        unreadable = self.root / "missing"

        result = self.promote(edge, unreadable)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be listed", result.stderr)
        self.assertFalse(unreadable.exists())

    def test_fills_an_empty_destination(self):
        edge = self.bucket("edge", OCT_20, OCT_20, release=2)
        empty = self.root / "empty"
        empty.mkdir()

        result = self.promote(edge, empty)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((empty / "lastsync").read_text(), f"{OCT_20}\n")


# What the hourly job publishes: the mirror's own /lastsync beside the
# repositories', in a step of its own once the databases are up.
@unittest.skipUnless(shutil.which("rclone"), "needs rclone")
class SyncArmTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "bin").mkdir()
        for script in ("omarchy-mirror-sync-arm", "omarchy-mirror-upload-arm"):
            shutil.copy(BIN / script, self.root / "bin")
        (self.root / "config").mkdir()
        (self.root / "config" / "arm-allow-null-records").write_text("")
        # The stager's part of a run: every repository staged with the run's
        # stamp, and the report upload-arm requires.
        stager = self.root / "bin/omarchy-mirror-stage-arm"
        stager.write_text(
            "#!/bin/bash\n"
            'while [[ $# -gt 0 ]]; do [[ $1 == --stage ]] && stage=$2; shift; done\n'
            "for repo in core extra alarm aur; do\n"
            '  mkdir -p "$stage/aarch64/$repo"\n'
            '  echo "$repo database" >"$stage/aarch64/$repo/$repo.db"\n'
            f'  echo {OCT_20} >"$stage/aarch64/$repo/lastsync"\n'
            "done\n"
            'printf \'{"status":"complete","signatures_verified":true,"selected_repositories":["aarch64/core","aarch64/extra","aarch64/alarm","aarch64/aur"]}\\n\' >"${stage%/}.status.json"\n'
        )
        stager.chmod(0o755)
        self.stage = self.root / "stage"
        self.bucket = self.root / "bucket"
        self.bucket.mkdir()

    def test_publishes_the_mirror_stamp_after_the_databases(self):
        log = self.root / "rclone.log"
        # rclone, recording which call published the root stamp.
        wrapper = self.root / "bin/rclone"
        wrapper.write_text(
            "#!/bin/bash\n"
            f'before=$([[ -f {self.bucket}/lastsync ]] && echo yes || echo no)\n'
            f'dbs=$([[ -f {self.bucket}/aarch64/core/core.db ]] && echo yes || echo no)\n'
            # The real rclone, found without this directory: a shim would find
            # this wrapper again.
            f'PATH="{os.environ["PATH"]}" rclone "$@"; status=$?\n'
            f'after=$([[ -f {self.bucket}/lastsync ]] && echo yes || echo no)\n'
            f'echo "$1 root:$before->$after dbs-before:$dbs" >>{log}\n'
            "exit $status\n"
        )
        wrapper.chmod(0o755)
        result = subprocess.run(
            [str(self.root / "bin/omarchy-mirror-sync-arm"), "--stage", f"{self.stage}/", "--r2", f"omarchy:{self.bucket}", "--keyring", "/dev/null"],
            capture_output=True,
            text=True,
            env={**rclone_env(self.root), "PATH": f"{self.root}/bin:{os.environ['PATH']}"},
        )
        status_report = self.root / "stage.status.json"

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout + (status_report.read_text() if status_report.exists() else ""))
        self.assertEqual((self.bucket / "lastsync").read_text(), f"{OCT_20}\n")
        calls = log.read_text().splitlines()
        published = [call for call in calls if "root:no->yes" in call]
        self.assertEqual(len(published), 1, calls)
        # Every database was up before the call that published the stamp began.
        self.assertIn("dbs-before:yes", published[0])
        self.assertEqual(calls.index(published[0]), 2, calls)


if __name__ == "__main__":
    unittest.main()
