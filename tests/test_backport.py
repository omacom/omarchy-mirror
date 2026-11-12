import hashlib
import io
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin"
FROZEN_SYNC = 1792286001
EDGE_SYNC = 1792528110
OLD = "ca-certificates-mozilla-3.131-1"
NEW = "ca-certificates-mozilla-3.131.1-1"


class BackportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "bin").mkdir()
        shutil.copy(BIN / "omarchy-mirror-backport", self.root / "bin")
        upload = self.root / "bin/omarchy-mirror-upload"
        upload.write_text('#!/bin/bash\necho "$*" >> "$(dirname "$0")/../uploads"\n')
        upload.chmod(0o755)

    def stage(self, name, lastsync, entries):
        """A stage whose core repository holds the given name-version-release
        entries, each with its package, signature and both database names."""
        repo = self.root / name / "core/os/x86_64"
        repo.mkdir(parents=True)
        for kind in ("db", "files"):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                for entry in entries:
                    package = f"{entry}-x86_64.pkg.tar.zst"
                    path = repo / package
                    if not path.exists():
                        path.write_text(f"{entry}\n")
                        (repo / f"{package}.sig").write_text(f"signature of {entry}\n")
                    sha = hashlib.sha256(path.read_bytes()).hexdigest()
                    members = {"desc": f"%FILENAME%\n{package}\n\n%NAME%\n{entry}\n\n%SHA256SUM%\n{sha}\n\n"}
                    if kind == "files":
                        members["files"] = f"%FILES%\nusr/share/{entry}\n\n"
                    for member, text in members.items():
                        data = text.encode()
                        info = tarfile.TarInfo(f"{entry}/{member}")
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
            (repo / f"core.{kind}").write_bytes(buffer.getvalue())
            (repo / f"core.{kind}.tar.gz").write_bytes(buffer.getvalue())
        (self.root / name / "lastsync").write_text(f"{lastsync}\n")
        return self.root / name

    def backport(self, source, target, package="ca-certificates-mozilla"):
        return subprocess.run(
            [
                str(self.root / "bin/omarchy-mirror-backport"),
                "--from", f"{source}/",
                "--to", f"{target}/",
                "--repo", "core",
                "--package", package,
                "--r2", "omarchy:target",
            ],
            capture_output=True,
            text=True,
        )

    def entries(self, stage, name="core.db"):
        with tarfile.open(stage / "core/os/x86_64" / name) as tar:
            return sorted(member.name for member in tar.getmembers() if member.isfile())

    def uploads(self):
        log = self.root / "uploads"
        return log.read_text().splitlines() if log.exists() else []

    def test_swaps_the_one_entry_and_publishes_it(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW, "glibc-2.43-2", "nss-3.131.1-1"])
        rc = self.stage("rc", FROZEN_SYNC, [OLD, "glibc-2.43-1", "nss-3.131-1"])
        source_desc = tarfile.open(edge / "core/os/x86_64/core.db").extractfile(f"{NEW}/desc").read()

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("core.db", "core.db.tar.gz"):
            self.assertEqual(self.entries(rc, name), [f"{NEW}/desc", "glibc-2.43-1/desc", "nss-3.131-1/desc"])
        for name in ("core.files", "core.files.tar.gz"):
            self.assertEqual(
                self.entries(rc, name),
                [f"{NEW}/desc", f"{NEW}/files", "glibc-2.43-1/desc", "glibc-2.43-1/files", "nss-3.131-1/desc", "nss-3.131-1/files"],
            )
        with tarfile.open(rc / "core/os/x86_64/core.db") as tar:
            self.assertEqual(tar.extractfile(f"{NEW}/desc").read(), source_desc)
        repo = rc / "core/os/x86_64"
        self.assertTrue((repo / f"{NEW}-x86_64.pkg.tar.zst").exists())
        self.assertTrue((repo / f"{NEW}-x86_64.pkg.tar.zst.sig").exists())
        self.assertTrue((repo / f"{OLD}-x86_64.pkg.tar.zst").exists())
        self.assertEqual((rc / "lastsync").read_text(), f"{FROZEN_SYNC}\n")
        self.assertIn(f"core {OLD} -> {NEW} from snapshot {EDGE_SYNC}", (rc / "backports").read_text())
        self.assertEqual(self.uploads(), [f"--stage {rc}/ --r2 omarchy:target"])

    def test_running_again_only_finishes_the_upload(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])
        self.backport(edge, rc)
        database = (rc / "core/os/x86_64/core.db").read_bytes()

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"already serves {NEW}", result.stdout)
        self.assertEqual((rc / "core/os/x86_64/core.db").read_bytes(), database)
        self.assertEqual(len((rc / "backports").read_text().splitlines()), 1)
        self.assertEqual(len(self.uploads()), 2)

    def test_replaces_a_damaged_copy_left_in_the_target(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])
        package = f"core/os/x86_64/{NEW}-x86_64.pkg.tar.zst"
        (rc / package).write_text("trunc")

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((rc / package).read_bytes(), (edge / package).read_bytes())

    def test_finishes_a_run_that_stopped_after_the_first_database(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW, "glibc-2.43-2"])
        rc = self.stage("rc", FROZEN_SYNC, [OLD, "glibc-2.43-1"])
        done = self.stage("done", FROZEN_SYNC, [OLD, "glibc-2.43-1"])
        self.backport(edge, done)
        shutil.copy(done / "core/os/x86_64/core.db", rc / "core/os/x86_64/core.db")
        (self.root / "uploads").unlink()

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        repo = rc / "core/os/x86_64"
        self.assertEqual((repo / "core.db").read_bytes(), (done / "core/os/x86_64/core.db").read_bytes())
        self.assertEqual((repo / "core.db.tar.gz").read_bytes(), (repo / "core.db").read_bytes())
        self.assertEqual((repo / "core.files.tar.gz").read_bytes(), (repo / "core.files").read_bytes())
        self.assertEqual(self.entries(rc, "core.files"), [f"{NEW}/desc", f"{NEW}/files", "glibc-2.43-1/desc", "glibc-2.43-1/files"])
        self.assertIn(f"core {OLD} -> {NEW}", (rc / "backports").read_text())
        self.assertEqual(self.uploads(), [f"--stage {rc}/ --r2 omarchy:target"])

    def test_never_takes_a_package_from_an_older_snapshot(self):
        stable = self.stage("stable", FROZEN_SYNC, [OLD])
        rc = self.stage("rc", EDGE_SYNC, [NEW])

        result = self.backport(stable, rc)

        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a newer snapshot", result.stderr)
        self.assertEqual(self.entries(rc), [f"{NEW}/desc"])
        self.assertEqual(self.uploads(), [])

    def test_never_adds_a_package_the_target_does_not_serve(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW, "glibc-2.43-2"])
        rc = self.stage("rc", FROZEN_SYNC, ["glibc-2.43-1"])

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 1)
        self.assertIn("never adds one", result.stderr)
        self.assertEqual(self.entries(rc), ["glibc-2.43-1/desc"])
        self.assertEqual(self.uploads(), [])

    def test_matches_only_the_named_package(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])

        result = self.backport(edge, rc, package="ca-certificates")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.entries(rc), [f"{OLD}/desc"])
        self.assertEqual(self.uploads(), [])

    def test_refuses_a_package_that_does_not_match_its_checksum(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])
        (edge / f"core/os/x86_64/{NEW}-x86_64.pkg.tar.zst").write_text("replaced by the hourly sync\n")

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match its checksum", result.stderr)
        self.assertEqual(self.entries(rc), [f"{OLD}/desc"])
        self.assertFalse((rc / f"core/os/x86_64/{NEW}-x86_64.pkg.tar.zst").exists())
        self.assertFalse((rc / "backports").exists())
        self.assertEqual(self.uploads(), [])


if __name__ == "__main__":
    unittest.main()
