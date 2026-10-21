import os
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin"


def tree(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@unittest.skipUnless(shutil.which("rclone") and shutil.which("vercmp"), "needs real rclone and vercmp")
class UpdateFloorTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        config = self.root / "rclone.conf"
        config.touch()
        self.env = {**os.environ, "RCLONE_CONFIG": str(config), "RCLONE_CONFIG_OMARCHY_TYPE": "local"}

    def stage(self, name, snapshot, floor=None, arm=False, version=None):
        root = self.root / name
        repo = root / ("aarch64/core" if arm else "core/os/x86_64")
        repo.mkdir(parents=True)
        version = version or ("2-1" if floor else "1-1")
        payload = version.encode()
        record = (f"%NAME%\ndemo\n\n%VERSION%\n{version}\n\n%SHA256SUM%\n{hashlib.sha256(payload).hexdigest()}\n\n").encode()
        with tarfile.open(repo / "core.db", "w:gz") as archive:
            item = tarfile.TarInfo(f"demo-{version}/desc")
            item.size = len(record)
            archive.addfile(item, io.BytesIO(record))
        (repo / "core.files").write_text(f"{name} files\n")
        (repo / "package.pkg.tar.xz").write_bytes(payload)
        (root / "lastsync").write_text(f"{snapshot}\n")
        if floor is not None:
            (root / "package-update-floor.json").write_text(json.dumps({
                "schema": 1, "architecture": "aarch64" if arm else "x86_64", "packages": {
                    "demo": {"version": floor, "sha256": hashlib.sha256(floor.encode()).hexdigest()}}}) + "\n")
        return root

    def remote(self, root):
        return f"omarchy:{root}"

    def run_script(self, name, *args):
        return subprocess.run([str(BIN / name), *map(str, args)], capture_output=True, text=True, env=self.env)

    def test_old_full_upload_cannot_remove_a_hotfix(self):
        stage = self.stage("stage", 42)
        target = self.stage("target", 42, "2-1")
        before = tree(target)
        result = self.run_script("omarchy-mirror-upload", "--stage", stage, "--r2", self.remote(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("older than targeted package updates", result.stderr)
        self.assertEqual(tree(target), before)

    @unittest.skipUnless(shutil.which("rsync"), "needs rsync")
    def test_stale_copy_is_rejected_before_overwriting_the_local_stage(self):
        source = self.stage("source", 42)
        destination = self.stage("local-stable", 42, "2-1")
        target = self.stage("target", 42, "2-1")
        before = tree(destination)
        result = self.run_script("omarchy-mirror-copy-stage", "--from", source, "--to", destination, "--r2", self.remote(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("older than targeted package updates", result.stderr)
        self.assertEqual(tree(destination), before)

    def test_fresh_snapshot_preserves_a_higher_existing_floor(self):
        stage = self.stage("stage", 101, "1-1", version="3-1")
        target = self.stage("target", 42, "2-1")
        result = self.run_script("omarchy-mirror-upload", "--stage", stage, "--r2", self.remote(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / "lastsync").read_text(), "101\n")
        floor = json.loads((target / "package-update-floor.json").read_text())
        self.assertEqual(floor["packages"]["demo"]["version"], "2-1")
        self.assertEqual((target / "core/os/x86_64/core.db").read_bytes(), (stage / "core/os/x86_64/core.db").read_bytes())

    def test_restored_patched_stage_carries_its_floor_to_an_empty_target(self):
        stage = self.stage("stage", 42, "2-1")
        target = self.root / "empty"
        target.mkdir()
        result = self.run_script("omarchy-mirror-upload", "--stage", stage, "--r2", self.remote(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / "lastsync").read_text(), "42\n")
        floor = json.loads((target / "package-update-floor.json").read_text())
        self.assertEqual(floor["packages"]["demo"]["version"], "2-1")

    def test_malformed_target_floor_fails_closed(self):
        stage = self.stage("stage", 101)
        target = self.stage("target", 42, "2-1")
        (target / "package-update-floor.json").write_text("invalid")
        before = tree(target)
        result = self.run_script("omarchy-mirror-upload", "--stage", stage, "--r2", self.remote(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unreadable", result.stderr)
        self.assertEqual(tree(target), before)

    def test_unreadable_destination_is_not_treated_as_having_no_floor(self):
        stage = self.stage("stage", 101)
        target = self.root / "not-a-directory"
        target.write_bytes(b"keep")
        result = self.run_script("omarchy-mirror-upload", "--stage", stage, "--r2", self.remote(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), b"keep")

    def test_arm_promotion_rejects_a_bucket_without_the_required_hotfix(self):
        source = self.stage("source", 42, arm=True)
        target = self.stage("target", 42, "2-1", arm=True)
        before = tree(target)
        result = self.run_script("omarchy-mirror-promote-arm", "--from", self.remote(source), "--to", self.remote(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("older than targeted package updates", result.stderr)
        self.assertEqual(tree(target), before)

    def test_arm_promotion_preserves_a_copied_package_update_floor(self):
        source = self.stage("source", 42, "2-1", arm=True)
        target = self.stage("target", 42, arm=True)
        result = self.run_script("omarchy-mirror-promote-arm", "--from", self.remote(source), "--to", self.remote(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        floor = json.loads((target / "package-update-floor.json").read_text())
        self.assertEqual(floor["packages"]["demo"]["version"], "2-1")
        self.assertEqual((target / "aarch64/core/core.db").read_bytes(), (source / "aarch64/core/core.db").read_bytes())

    def test_a_new_timestamp_does_not_allow_old_package_versions(self):
        stage = self.stage("stage", 999, version="1-1")
        target = self.stage("target", 42, "2-1")
        before = tree(target)
        result = self.run_script("omarchy-mirror-upload", "--stage", stage, "--r2", self.remote(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("older than targeted package updates", result.stderr)
        self.assertEqual(tree(target), before)

    def test_same_version_with_different_contents_is_rejected(self):
        stage = self.stage("stage", 999, version="2-1")
        target = self.stage("target", 42, "2-1")
        floor_file = target / "package-update-floor.json"
        floor = json.loads(floor_file.read_text())
        floor["packages"]["demo"]["sha256"] = "0" * 64
        floor_file.write_text(json.dumps(floor))
        before = tree(target)
        result = self.run_script("omarchy-mirror-upload", "--stage", stage, "--r2", self.remote(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr)
        self.assertEqual(tree(target), before)


if __name__ == "__main__":
    unittest.main()
