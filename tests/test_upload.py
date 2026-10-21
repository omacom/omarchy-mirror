from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


UPLOAD = Path(__file__).resolve().parents[1] / "bin/omarchy-mirror-upload"


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.log = self.root / "rclone.log"
        (self.root / "bin").mkdir()

    def upload(self, failing_step):
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
            [str(UPLOAD), "--stage", f"{self.root}/stage/", "--r2", "omarchy:target"],
            capture_output=True,
            text=True,
            env={"PATH": f"{self.root}/bin:/usr/bin:/bin"},
        )

    def calls(self):
        return [line.split()[0] for line in self.log.read_text().splitlines()]

    def test_runs_every_step(self):
        result = self.upload(failing_step="never")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["lsf", "copy", "copy", "copy", "sync", "lsf"])

    def test_a_failed_database_upload_stops_before_the_prune(self):
        result = self.upload(failing_step="--include **/*.db*")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["lsf", "copy", "copy", "copy"])


if __name__ == "__main__":
    unittest.main()
