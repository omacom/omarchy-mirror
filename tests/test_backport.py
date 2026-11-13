import hashlib
import io
import os
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
PIN = "core/ca-certificates-mozilla=3.131.1-1"


def split(entry):
    """name-version-release into (name, version-release)."""
    name, version, release = entry.rsplit("-", 2)
    return name, f"{version}-{release}"


class BackportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "bin").mkdir()
        for tool in ("omarchy-mirror-backport", "omarchy-mirror-backwards"):
            shutil.copy(BIN / tool, self.root / "bin")
        upload = self.root / "bin/omarchy-mirror-upload"
        upload.write_text('#!/bin/bash\necho "$*" >> "$(dirname "$0")/../uploads"\n')
        upload.chmod(0o755)

    def stage(self, name, lastsync, entries):
        """A stage whose repositories hold the given name-version-release
        entries, each with its package, signature and both database names.
        A list is core's entries; a dict maps each repository to its own."""
        repos = entries if isinstance(entries, dict) else {"core": entries}
        for repo_name, repo_entries in repos.items():
            repo = self.root / name / repo_name / "os/x86_64"
            repo.mkdir(parents=True)
            for kind in ("db", "files"):
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                    for entry in repo_entries:
                        package = f"{entry}-x86_64.pkg.tar.zst"
                        path = repo / package
                        if not path.exists():
                            path.write_text(f"{entry}\n")
                            (repo / f"{package}.sig").write_text(f"signature of {entry}\n")
                        sha = hashlib.sha256(path.read_bytes()).hexdigest()
                        package_name, version = split(entry)
                        members = {
                            "desc": f"%FILENAME%\n{package}\n\n%NAME%\n{package_name}\n\n%VERSION%\n{version}\n\n%SHA256SUM%\n{sha}\n\n"
                        }
                        if kind == "files":
                            members["files"] = f"%FILES%\nusr/share/{entry}\n\n"
                        for member, text in members.items():
                            data = text.encode()
                            info = tarfile.TarInfo(f"{entry}/{member}")
                            info.size = len(data)
                            tar.addfile(info, io.BytesIO(data))
                (repo / f"{repo_name}.{kind}").write_bytes(buffer.getvalue())
                (repo / f"{repo_name}.{kind}.tar.gz").write_bytes(buffer.getvalue())
        (self.root / name / "lastsync").write_text(f"{lastsync}\n")
        return self.root / name

    def backport(self, source, target, *packages, cwd=None, env=None):
        command = [str(self.root / "bin/omarchy-mirror-backport"), "--from", f"{source}/", "--to", f"{target}/", "--r2", "omarchy:target"]
        for package in packages or (PIN,):
            command += ["--package", package]
        return subprocess.run(command, capture_output=True, text=True, cwd=cwd, env=env)

    def stop_before(self, suffix):
        """An environment whose mv fails on a path ending in suffix, as if the
        run stopped there."""
        wrappers = self.root / "wrappers"
        wrappers.mkdir(exist_ok=True)
        mv = wrappers / "mv"
        mv.write_text(f'#!/bin/bash\n[[ $1 == *{suffix} ]] && exit 1\nexec /bin/mv "$@"\n')
        mv.chmod(0o755)
        return {**os.environ, "PATH": f"{wrappers}:{os.environ['PATH']}"}

    def entries(self, stage, name="core.db", repo="core"):
        with tarfile.open(stage / repo / "os/x86_64" / name) as tar:
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

    def test_publishes_a_set_across_repositories_in_one_upload(self):
        edge = self.stage("edge", EDGE_SYNC, {
            "core": ["brotli-1.2.1-1", "brotli-testdata-1.2.1-1", "glibc-2.43-2"],
            "extra": ["chromium-157.0.8103.38-1", "firefox-160.0-1"],
            "multilib": ["lib32-brotli-1.2.1-1"],
        })
        rc = self.stage("rc", FROZEN_SYNC, {
            "core": ["brotli-1.2.0-1", "brotli-testdata-1.2.0-1", "glibc-2.43-1"],
            "extra": ["chromium-155.0.8059.84-1", "firefox-158.0.1-1"],
            "multilib": ["lib32-brotli-1.2.0-2"],
        })

        result = self.backport(
            edge, rc, "core/brotli=1.2.1-1", "extra/chromium=157.0.8103.38-1", "extra/firefox=160.0-1", "multilib/lib32-brotli=1.2.1-1"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        # brotli is matched whole: brotli-testdata, not named, stays as it was.
        self.assertEqual(self.entries(rc), ["brotli-1.2.1-1/desc", "brotli-testdata-1.2.0-1/desc", "glibc-2.43-1/desc"])
        self.assertEqual(self.entries(rc, "extra.db", "extra"), ["chromium-157.0.8103.38-1/desc", "firefox-160.0-1/desc"])
        self.assertEqual(self.entries(rc, "multilib.files.tar.gz", "multilib"), ["lib32-brotli-1.2.1-1/desc", "lib32-brotli-1.2.1-1/files"])
        self.assertEqual(len((rc / "backports").read_text().splitlines()), 4)
        self.assertEqual(self.uploads(), [f"--stage {rc}/ --r2 omarchy:target"])

    def test_refuses_a_version_other_than_the_tested_one(self):
        # The hourly sync moved edge on after the VM proof of 3.131.1-1.
        edge = self.stage("edge", EDGE_SYNC, ["ca-certificates-mozilla-3.131.2-1"])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 1)
        self.assertIn("serves ca-certificates-mozilla-3.131.2-1 in core, not the tested", result.stderr)
        self.assertEqual(self.entries(rc), [f"{OLD}/desc"])
        self.assertFalse((rc / "backports").exists())
        self.assertEqual(self.uploads(), [])

    def test_one_package_that_fails_its_checks_stops_the_whole_set(self):
        edge = self.stage("edge", EDGE_SYNC, {"core": [NEW], "extra": ["firefox-160.0-1"]})
        rc = self.stage("rc", FROZEN_SYNC, {"core": [OLD], "extra": ["firefox-158.0.1-1"]})
        (edge / "extra/os/x86_64/firefox-160.0-1-x86_64.pkg.tar.zst").write_text("replaced by the hourly sync\n")
        database = (rc / "core/os/x86_64/core.db").read_bytes()

        result = self.backport(edge, rc, PIN, "extra/firefox=160.0-1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match its checksum", result.stderr)
        self.assertEqual((rc / "core/os/x86_64/core.db").read_bytes(), database)
        self.assertFalse((rc / f"core/os/x86_64/{NEW}-x86_64.pkg.tar.zst").exists())
        self.assertFalse((rc / "backports").exists())
        self.assertEqual(self.uploads(), [])

    def test_never_moves_a_package_back(self):
        # rc already has 3.131.2-1 backported; a newer snapshot that serves
        # 3.131.1-1 (upstream reverted it) must not take it back.
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, ["ca-certificates-mozilla-3.131.2-1"])

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 1)
        self.assertIn("core ca-certificates-mozilla 3.131.2-1 -> 3.131.1-1", result.stderr)
        self.assertEqual(self.entries(rc), ["ca-certificates-mozilla-3.131.2-1/desc"])
        self.assertEqual(self.uploads(), [])

    def test_takes_stage_paths_relative_to_the_working_directory(self):
        self.stage("edge", EDGE_SYNC, [NEW])
        self.stage("rc", FROZEN_SYNC, [OLD])

        result = self.backport("edge", "rc", cwd=self.root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entries(self.root / "rc"), [f"{NEW}/desc"])
        self.assertEqual(self.uploads(), [f"--stage {self.root}/rc/ --r2 omarchy:target"])

    def test_a_name_that_prefixes_the_last_entry_still_matches(self):
        # brotli-testdata, listed last, shares brotli's prefix; the match
        # before it must stand.
        edge = self.stage("edge", EDGE_SYNC, ["brotli-1.2.1-1", "brotli-testdata-1.2.1-1"])
        rc = self.stage("rc", FROZEN_SYNC, ["brotli-1.2.0-1", "brotli-testdata-1.2.0-1"])

        result = self.backport(edge, rc, "core/brotli=1.2.1-1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entries(rc), ["brotli-1.2.1-1/desc", "brotli-testdata-1.2.0-1/desc"])

    def test_running_again_finishes_the_upload_after_the_source_moved_on(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])
        self.backport(edge, rc)
        database = (rc / "core/os/x86_64/core.db").read_bytes()
        shutil.rmtree(edge)
        edge = self.stage("edge", EDGE_SYNC + 3600, ["ca-certificates-mozilla-3.131.2-1"])

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"already serves {NEW}", result.stdout)
        self.assertEqual((rc / "core/os/x86_64/core.db").read_bytes(), database)
        self.assertEqual(len(self.uploads()), 2)

    def test_a_damaged_copy_of_the_pinned_version_comes_from_the_source_again(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])
        self.backport(edge, rc)
        package = f"core/os/x86_64/{NEW}-x86_64.pkg.tar.zst"
        (rc / package).write_text("trunc")

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((rc / package).read_bytes(), (edge / package).read_bytes())

    def test_a_run_that_stopped_between_databases_finishes_after_the_source_moved_on(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])
        stopped = self.backport(edge, rc, env=self.stop_before(".core.files.new"))
        self.assertNotEqual(stopped.returncode, 0)
        self.assertEqual(self.entries(rc), [f"{NEW}/desc"])
        self.assertEqual(self.entries(rc, "core.files"), [f"{OLD}/desc", f"{OLD}/files"])
        self.assertEqual(self.uploads(), [])
        shutil.rmtree(edge)
        edge = self.stage("edge", EDGE_SYNC + 3600, ["ca-certificates-mozilla-3.131.2-1"])

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Finishing the backport", result.stdout)
        self.assertEqual(self.entries(rc, "core.files"), [f"{NEW}/desc", f"{NEW}/files"])
        self.assertEqual(self.entries(rc, "core.files.tar.gz"), [f"{NEW}/desc", f"{NEW}/files"])
        self.assertEqual(len((rc / "backports").read_text().splitlines()), 1)
        self.assertFalse(Path(f"{rc}.backport-pending").exists())
        self.assertEqual(self.uploads(), [f"--stage {rc}/ --r2 omarchy:target"])

    def test_an_unfinished_backport_holds_back_any_other_set(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW, "glibc-2.43-2"])
        rc = self.stage("rc", FROZEN_SYNC, [OLD, "glibc-2.43-1"])
        self.backport(edge, rc, env=self.stop_before(".core.files.new"))

        result = self.backport(edge, rc, "core/glibc=2.43-2")

        self.assertEqual(result.returncode, 1)
        self.assertIn("unfinished backport of another set", result.stderr)
        self.assertIn(PIN, result.stderr)
        self.assertEqual(self.uploads(), [])

    def test_a_pending_backport_never_lands_on_another_snapshot(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])
        self.backport(edge, rc, env=self.stop_before(".core.files.new"))
        # A fresh rc snapshot arrived since.
        (rc / "lastsync").write_text(f"{EDGE_SYNC + 7200}\n")
        files = (rc / "core/os/x86_64/core.files").read_bytes()

        result = self.backport(edge, rc)

        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer holds the snapshot", result.stderr)
        self.assertEqual((rc / "core/os/x86_64/core.files").read_bytes(), files)
        self.assertEqual(self.uploads(), [])

    def test_a_name_in_two_repositories_does_not_block_another_package(self):
        # foo is in core at 2-1 and in extra at 1-1; machines get core's.
        # Backporting firefox in extra leaves foo as it is.
        edge = self.stage("edge", EDGE_SYNC, {"core": ["foo-2-1"], "extra": ["foo-1-1", "firefox-160.0-1"]})
        rc = self.stage("rc", FROZEN_SYNC, {"core": ["foo-2-1"], "extra": ["foo-1-1", "firefox-158.0.1-1"]})

        result = self.backport(edge, rc, "extra/firefox=160.0-1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entries(rc, "extra.db", "extra"), ["firefox-160.0-1/desc", "foo-1-1/desc"])

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

        result = self.backport(stable, rc, "core/ca-certificates-mozilla=3.131-1")

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

        result = self.backport(edge, rc, "core/ca-certificates=3.131.1-1")

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

    def test_refuses_a_package_named_without_a_version(self):
        edge = self.stage("edge", EDGE_SYNC, [NEW])
        rc = self.stage("rc", FROZEN_SYNC, [OLD])

        result = self.backport(edge, rc, "core/ca-certificates-mozilla")

        self.assertEqual(result.returncode, 1)
        self.assertIn("is not REPO/NAME=VERSION", result.stderr)
        self.assertEqual(self.uploads(), [])


if __name__ == "__main__":
    unittest.main()
