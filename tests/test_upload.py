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

    def lines(self):
        return self.log.read_text().splitlines()

    def test_runs_every_step(self):
        result = self.upload(failing_step="never")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["copy", "copy", "copy", "copy", "sync"])

    def test_the_stamps_go_up_alone_after_the_databases(self):
        # A client or a downstream mirror reads lastsync to learn which
        # snapshot is served, so it must never arrive before the databases.
        result = self.upload(failing_step="never")

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.lines()
        databases = [i for i, line in enumerate(lines) if "--include **/*.db*" in line]
        stamps = [i for i, line in enumerate(lines) if "--include /lastsync" in line]
        self.assertEqual(len(databases), 1)
        self.assertEqual(len(stamps), 1)
        self.assertLess(databases[0], stamps[0])
        self.assertNotIn("lastsync", lines[databases[0]])
        self.assertNotIn("lastupdate", lines[databases[0]])
        self.assertNotIn(".db", lines[stamps[0]])

    def test_a_failed_database_upload_stops_before_the_stamps(self):
        result = self.upload(failing_step="--include **/*.db*")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), ["copy", "copy", "copy"])
        self.assertFalse(any("--include /lastsync" in line for line in self.lines()))


if __name__ == "__main__":
    unittest.main()
