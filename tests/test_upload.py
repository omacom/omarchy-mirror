from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from test_verify import stage_repo


UPLOAD = Path(__file__).resolve().parents[1] / "bin/omarchy-mirror-upload"


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.log = self.root / "rclone.log"
        (self.root / "bin").mkdir()

    def upload(self, failing_step, *args):
        # rclone stand-in: record each call, and fail the one that publishes
        # the databases when asked to.
        rclone = self.root / "bin/rclone"
        rclone.write_text(
            "#!/bin/bash\n"
            f'echo "$1 $*" >> "{self.log}"\n'
            f'[[ "$*" == *"{failing_step}"* ]] && exit 1\n'
            "exit 0\n"
        )
        rclone.chmod(0o755)
        return subprocess.run(
            [str(UPLOAD), "--stage", f"{self.root}/stage/", "--r2", "omarchy:target", *args],
            capture_output=True,
            text=True,
            env={"PATH": f"{self.root}/bin:/usr/bin:/bin:/sbin"},
        )

    def calls(self):
        return [line.split()[0] for line in self.log.read_text().splitlines()]

    def test_runs_every_step(self):
        result = self.upload(failing_step="never")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["copy", "copy", "copy", "sync"])
        self.assertIn("omarchy:target has no public URL", result.stdout)

    def test_a_failed_database_upload_stops_before_the_prune(self):
        result = self.upload(failing_step="--include **/*.db*")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["copy", "copy", "copy"])

    def test_a_package_that_does_not_read_back_stops_before_the_databases(self):
        # The stand-in uploads nothing, so the public URL never has the package.
        stage_repo(self.root / "stage", {"a-1-1-x86_64.pkg.tar.zst": b"a"})
        handler = partial(SimpleHTTPRequestHandler, directory=str(self.root / "bin"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        result = self.upload("never", "--url", f"http://127.0.0.1:{server.server_port}")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["copy", "copy", "copy"])
        self.assertIn("--files-from", self.log.read_text().splitlines()[2])
        self.assertNotIn("--include **/*.db*", self.log.read_text())
        self.assertIn("core/os/x86_64/a-1-1-x86_64.pkg.tar.zst", result.stderr)


if __name__ == "__main__":
    unittest.main()
