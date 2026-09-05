import base64
from collections import Counter
from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import runpy
import subprocess
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin/omarchy-mirror-stage-arm"
API = runpy.run_path(str(SCRIPT))
PREFIX = "/aarch64/core/"


def database(records, record_names=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index, (name, body, signature) in enumerate(records):
            desc = (f"%FILENAME%\n{name}\n\n%CSIZE%\n{len(body)}\n\n"
                    f"%SHA256SUM%\n{hashlib.sha256(body).hexdigest()}\n\n"
                    f"%PGPSIG%\n{base64.b64encode(signature).decode()}\n\n").encode()
            record_name = record_names[index] if record_names else f"package-{index}"
            member = tarfile.TarInfo(f"{record_name}/desc")
            member.size = len(desc)
            archive.addfile(member, io.BytesIO(desc))
    return buffer.getvalue()


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.stage = Path(self.temporary.name) / "stage"
        self.files = {}
        self.requests = Counter()
        self.hook = lambda path: None
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import unquote
                path = unquote(self.path)
                test.requests[path] += 1
                test.hook(path)
                if path not in test.files:
                    self.send_error(404)
                    return
                body = test.files[path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.upstream = f"http://127.0.0.1:{server.server_port}"
        self.first = ("demo-1-1-aarch64.pkg.tar.xz", b"first package", b"original signature")
        self.publish([self.first])

    def publish(self, records):
        data = database(records)
        self.files[PREFIX + "core.db"] = data
        self.files[PREFIX + "core.files"] = data
        for name, body, _ in records:
            self.files[PREFIX + name] = body

    def run_sync(self, *flags, success=True):
        result = subprocess.run(
            [str(SCRIPT), "--stage", str(self.stage), "--upstream", self.upstream,
             "--repos", "core", "--retries", "1", "--timeout", "2", *flags],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
        return result

    def test_full_sync_noop_and_corruption_repair(self):
        self.run_sync()
        repo = self.stage / "aarch64/core"
        name, body, signature = self.first
        self.assertEqual((repo / name).read_bytes(), body)
        self.assertEqual((repo / (name + ".sig")).read_bytes(), signature)
        self.assertEqual((repo / "core.db").read_bytes(), self.files[PREFIX + "core.db"])
        self.assertEqual((repo / "core.files.tar.gz").read_bytes(), self.files[PREFIX + "core.files"])
        self.assertTrue((self.stage / "lastsync").is_file())
        self.requests.clear()
        self.run_sync()
        self.assertEqual(self.requests[PREFIX + name], 0)
        # Same-size corruption must be detected, including on incremental runs.
        (repo / name).write_bytes(b"x" * len(body))
        (repo / (name + ".sig")).write_bytes(b"bad signature")
        self.run_sync()
        self.assertEqual(self.requests[PREFIX + name], 1)
        self.assertEqual((repo / name).read_bytes(), body)
        self.assertEqual((repo / (name + ".sig")).read_bytes(), signature)

    def test_incremental_download_prune_and_epoch_filename(self):
        self.run_sync()
        second = ("demo-2:2-1-aarch64.pkg.tar.xz", b"second package", b"second signature")
        self.publish([second])
        self.requests.clear()
        unrelated = self.stage / "aarch64/core/notes.txt"
        unrelated.write_text("keep")
        self.run_sync("--prune")
        self.assertEqual(self.requests[PREFIX + second[0]], 1)
        self.assertEqual(self.requests[PREFIX + self.first[0]], 0)
        self.assertFalse((self.stage / "aarch64/core" / self.first[0]).exists())
        self.assertFalse((self.stage / "aarch64/core" / (self.first[0] + ".sig")).exists())
        self.assertEqual(unrelated.read_text(), "keep")

    def test_failed_download_preserves_metadata_and_freshness(self):
        self.run_sync()
        old_db = (self.stage / "aarch64/core/core.db").read_bytes()
        marker = self.stage / "lastsync"
        marker.write_text("old freshness")
        for failure in ("404", "checksum"):
            with self.subTest(failure=failure):
                second = ("demo-2-1-aarch64.pkg.tar.xz", b"new package", b"new signature")
                self.publish([second])
                if failure == "404":
                    del self.files[PREFIX + second[0]]
                else:
                    self.files[PREFIX + second[0]] = b"x" * len(second[1])
                self.run_sync("--prune", success=False)
                self.assertEqual((self.stage / "aarch64/core/core.db").read_bytes(), old_db)
                self.assertEqual(marker.read_text(), "old freshness")
                self.assertTrue((self.stage / "aarch64/core" / self.first[0]).exists())
                self.assertFalse(list(self.stage.rglob("*.part")))

    def test_upstream_changes_do_not_publish_but_downloads_are_reused(self):
        replacement = database([("other-1-1-aarch64.pkg.tar.xz", b"other", b"sig")])

        def change(path):
            if path == PREFIX + "core.db" and self.requests[path] == 2:
                self.files[path] = replacement

        self.hook = change
        result = self.run_sync(success=False)
        self.assertIn("upstream database changed", result.stderr)
        self.assertFalse((self.stage / "lastsync").exists())
        self.assertFalse((self.stage / "aarch64/core/core.db").exists())
        self.assertTrue((self.stage / "aarch64/core" / self.first[0]).exists())
        self.hook = lambda path: None
        self.publish([self.first])
        self.requests.clear()
        self.run_sync()
        self.assertEqual(self.requests[PREFIX + self.first[0]], 0)

    def test_db_files_mismatch_fails_before_packages(self):
        self.files[PREFIX + "core.files"] = database([(self.first[0], b"other", b"sig")])
        result = self.run_sync(success=False)
        self.assertIn(".db and .files disagree", result.stderr)
        self.assertEqual(self.requests[PREFIX + self.first[0]], 0)
        self.assertFalse((self.stage / "lastsync").exists())

    def test_files_only_package_is_reported_and_not_staged(self):
        orphan = ("orphan-1-1-any.pkg.tar.xz", b"orphan", b"signature")
        self.files[PREFIX + "core.files"] = database([self.first, orphan])

        result = self.run_sync()

        self.assertIn(f"WARNING: core.files references {orphan[0]}", result.stdout)
        self.assertIn("not staging it", result.stdout)
        self.assertEqual(self.requests[PREFIX + orphan[0]], 0)
        self.assertFalse((self.stage / "aarch64/core" / orphan[0]).exists())

    def test_older_packages_missing_from_files_db_are_still_downloaded(self):
        second = ("legacy-1-1-any.pkg.tar.xz", b"legacy package", b"legacy signature")
        self.publish([self.first, second])
        self.files[PREFIX + "core.files"] = database([self.first])
        result = self.run_sync()
        self.assertIn("upstream .files omits 1 packages", result.stdout)
        self.assertEqual((self.stage / "aarch64/core" / second[0]).read_bytes(), second[1])
        self.assertEqual((self.stage / "aarch64/core/core.files").read_bytes(), self.files[PREFIX + "core.files"])

    def test_dry_run_writes_nothing(self):
        result = self.run_sync("--dry-run")
        self.assertIn("1 downloads", result.stdout)
        self.assertFalse(self.stage.exists())
        self.assertFalse(self.stage.with_suffix(".lock").exists())
        self.assertEqual(self.requests[PREFIX + self.first[0]], 0)

    def test_bad_database_and_path_traversal_fail_closed(self):
        null_record = io.BytesIO()
        with tarfile.open(fileobj=null_record, mode="w:gz") as archive:
            member = tarfile.TarInfo("findnewest-0.3-4/desc")
            member.size = 1271
            archive.addfile(member, io.BytesIO(b"\0" * member.size))
        invalid = [b"not an archive", database([]),
                   null_record.getvalue(),
                   database([("../../escape.pkg.tar.xz", b"bad", b"sig")])]
        for data in invalid:
            with self.subTest(data=data[:20]):
                self.files[PREFIX + "core.db"] = data
                self.run_sync("--prune", success=False)
                self.assertFalse((self.stage / "lastsync").exists())
                self.assertEqual(self.requests[PREFIX + self.first[0]], 0)

    def test_malformed_db_record_recovers_from_exact_files_record(self):
        record_name = "findnewest-0.3-4"
        null_record = io.BytesIO()
        with tarfile.open(fileobj=null_record, mode="w:gz") as archive:
            member = tarfile.TarInfo(f"{record_name}/desc")
            member.size = 1271
            archive.addfile(member, io.BytesIO(b"\0" * member.size))
        self.files[PREFIX + "core.db"] = null_record.getvalue()
        self.files[PREFIX + "core.files"] = database([self.first], [record_name])

        result = self.run_sync()

        self.assertIn("WARNING: recovered malformed core.db record", result.stdout)
        self.assertEqual((self.stage / "aarch64/core" / self.first[0]).read_bytes(), self.first[1])
        self.assertEqual((self.stage / "aarch64/core/core.db").read_bytes(), null_record.getvalue())

    def test_recovered_files_database_change_fails_before_publish(self):
        record_name = "findnewest-0.3-4"
        null_record = io.BytesIO()
        with tarfile.open(fileobj=null_record, mode="w:gz") as archive:
            member = tarfile.TarInfo(f"{record_name}/desc")
            member.size = 1271
            archive.addfile(member, io.BytesIO(b"\0" * member.size))
        self.files[PREFIX + "core.db"] = null_record.getvalue()
        self.files[PREFIX + "core.files"] = database([self.first], [record_name])

        def change(path):
            if path == PREFIX + "core.files" and self.requests[path] == 2:
                self.files[path] = database([("other-1-1-aarch64.pkg.tar.xz",
                                              b"other", b"signature")], [record_name])

        self.hook = change
        result = self.run_sync(success=False)

        self.assertIn("upstream files database changed", result.stderr)
        self.assertFalse((self.stage / "lastsync").exists())
        self.assertFalse((self.stage / "aarch64/core/core.db").exists())

    def test_database_signatures_are_preserved_and_removed_when_absent_upstream(self):
        for kind in ("db", "files"):
            self.files[PREFIX + f"core.{kind}.sig"] = b"metadata signature"
        self.run_sync()
        repo = self.stage / "aarch64/core"
        for kind in ("db", "files"):
            self.assertEqual((repo / f"core.{kind}.sig").read_bytes(), b"metadata signature")
            self.assertEqual((repo / f"core.{kind}.tar.gz.sig").read_bytes(), b"metadata signature")
            del self.files[PREFIX + f"core.{kind}.sig"]
        self.run_sync()
        self.assertFalse((repo / "core.db.sig").exists())
        self.assertFalse((repo / "core.files.tar.gz.sig").exists())

    def test_insufficient_space_fails_before_downloads(self):
        args = SimpleNamespace(stage=self.stage, upstream=self.upstream,
                               arch=["aarch64"], repos=["core"], dry_run=False,
                               timeout=2, retries=1)
        with patch("shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(API["SyncError"], "Insufficient disk space"):
                    API["sync"](args)
        self.assertEqual(self.requests[PREFIX + self.first[0]], 0)

    def test_lock_prevents_overlapping_sync(self):
        with API["stage_lock"](self.stage):
            result = self.run_sync(success=False)
        self.assertIn("Another ARM sync", result.stderr)
        self.assertEqual(sum(self.requests.values()), 0)


if __name__ == "__main__":
    unittest.main()
