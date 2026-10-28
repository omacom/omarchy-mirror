from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib
import io
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest


VERIFY = Path(__file__).resolve().parents[1] / "bin/omarchy-mirror-verify"
RCLONE = shutil.which("rclone")


def stage_repo(stage, packages, folder="core/os/x86_64", db="core.db", zstd=False):
    """Write packages into a stage folder and a database naming them."""
    folder = stage / folder
    folder.mkdir(parents=True, exist_ok=True)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w" if zstd else "w:gz") as tar:
        for name, data in packages.items():
            (folder / name).write_bytes(data)
            desc = f"%FILENAME%\n{name}\n\n%SHA256SUM%\n{hashlib.sha256(data).hexdigest()}\n\n".encode()
            entry = tarfile.TarInfo(f"{name.removesuffix('.pkg.tar.zst')}/desc")
            entry.size = len(desc)
            tar.addfile(entry, io.BytesIO(desc))
    data = archive.getvalue()
    if zstd:
        data = subprocess.run(["zstd", "-cq"], input=data, stdout=subprocess.PIPE, check=True).stdout
    (folder / db).write_bytes(data)


class Bucket(SimpleHTTPRequestHandler):
    """Serves a directory like a public bucket, but stalls the paths in stalled
    after their first 64 KiB, as R2's custom domain did."""

    def __init__(self, *args, stalled, requests, **kwargs):
        self.stalled, self.requests = stalled, requests
        super().__init__(*args, **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0].lstrip("/")
        self.requests.append(path)
        if path not in self.stalled:
            return super().do_GET()
        data = Path(self.translate_path(self.path)).read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data[:65536])
        self.wfile.flush()
        time.sleep(10)

    def log_message(self, *args):
        pass


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.stage = self.root / "stage"
        self.bucket = self.root / "bucket"
        self.packages = {
            "a-1-1-x86_64.pkg.tar.zst": b"a" * 200_000,
            "b-1-1-x86_64.pkg.tar.zst": b"b" * 300_000,
        }
        stage_repo(self.stage, self.packages)
        shutil.copytree(self.stage, self.bucket)

        self.stalled, self.requests = set(), []
        handler = partial(Bucket, directory=str(self.bucket), stalled=self.stalled, requests=self.requests)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.host = f"127.0.0.1:{server.server_port}"
        self.ledger = Path(f"{self.stage}.verified.{self.host}")

    def verify(self, *args):
        return subprocess.run(
            [str(VERIFY), "--stage", f"{self.stage}/", "--r2", str(self.bucket),
             "--url", f"http://{self.host}/", "--timeout", "2", *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def recorded(self):
        return sorted(line.split()[1] for line in self.ledger.read_text().splitlines())

    def package_reads(self):
        return [path for path in self.requests if path.endswith(".zst")]

    def test_records_packages_that_read_back(self):
        result = self.verify()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded(), [f"core/os/x86_64/{name}" for name in sorted(self.packages)])

    def test_a_stalled_package_fails_and_the_rest_are_recorded(self):
        self.stalled.add("core/os/x86_64/b-1-1-x86_64.pkg.tar.zst")

        result = self.verify()

        self.assertEqual(result.returncode, 1)
        self.assertIn("core/os/x86_64/b-1-1-x86_64.pkg.tar.zst", result.stderr)
        self.assertEqual(self.recorded(), ["core/os/x86_64/a-1-1-x86_64.pkg.tar.zst"])

    def test_a_package_missing_from_the_bucket_fails(self):
        (self.bucket / "core/os/x86_64/a-1-1-x86_64.pkg.tar.zst").unlink()

        result = self.verify()

        self.assertEqual(result.returncode, 1)
        self.assertIn("core/os/x86_64/a-1-1-x86_64.pkg.tar.zst", result.stderr)

    def test_a_package_missing_from_the_stage_fails_before_reading(self):
        (self.stage / "core/os/x86_64/a-1-1-x86_64.pkg.tar.zst").unlink()

        result = self.verify()

        self.assertEqual(result.returncode, 1)
        self.assertIn("not in the stage", result.stderr)
        self.assertEqual(self.package_reads(), [])

    def test_recorded_packages_are_not_read_again_unless_all(self):
        self.assertEqual(self.verify().returncode, 0)
        self.requests.clear()

        self.assertEqual(self.verify().returncode, 0)
        self.assertEqual(self.package_reads(), [])

        self.assertEqual(self.verify("--all").returncode, 0)
        self.assertEqual(len(self.package_reads()), 2)

    def test_all_drops_a_recorded_package_that_now_fails(self):
        self.assertEqual(self.verify().returncode, 0)
        self.stalled.add("core/os/x86_64/a-1-1-x86_64.pkg.tar.zst")

        self.assertEqual(self.verify("--all").returncode, 1)
        self.assertEqual(self.recorded(), ["core/os/x86_64/b-1-1-x86_64.pkg.tar.zst"])

    @unittest.skipUnless(shutil.which("zstd"), "needs zstd")
    def test_reads_a_zstd_database(self):
        # arch-mact2's database is zstd, with its own layout.
        stage_repo(self.stage, {"c-1-1-x86_64.pkg.tar.zst": b"c" * 1000}, folder="x86_64", db="arch-mact2.db", zstd=True)
        shutil.copytree(self.stage / "x86_64", self.bucket / "x86_64")

        result = self.verify()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("x86_64/c-1-1-x86_64.pkg.tar.zst", self.recorded())

    @unittest.skipUnless(RCLONE, "needs rclone")
    def test_repair_re_uploads_a_package_with_other_bytes(self):
        broken = self.bucket / "core/os/x86_64/a-1-1-x86_64.pkg.tar.zst"
        broken.write_bytes(b"x" * 200_000)

        result = self.verify("--repair")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(broken.read_bytes(), self.packages["a-1-1-x86_64.pkg.tar.zst"])
        self.assertEqual(len(self.recorded()), 2)

    def test_repair_refuses_more_than_max_repair(self):
        broken = self.bucket / "core/os/x86_64/a-1-1-x86_64.pkg.tar.zst"
        broken.write_bytes(b"x" * 200_000)

        result = self.verify("--repair", "--max-repair", "0")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(broken.read_bytes(), b"x" * 200_000)

    def test_repair_refuses_a_stage_copy_that_differs_from_its_database(self):
        (self.bucket / "core/os/x86_64/a-1-1-x86_64.pkg.tar.zst").write_bytes(b"x" * 200_000)
        (self.stage / "core/os/x86_64/a-1-1-x86_64.pkg.tar.zst").write_bytes(b"y" * 200_000)

        result = self.verify("--repair")

        self.assertEqual(result.returncode, 1)
        self.assertIn("stage's copy differs", result.stderr)


if __name__ == "__main__":
    unittest.main()
