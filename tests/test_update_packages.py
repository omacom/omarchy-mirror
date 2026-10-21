import base64
import functools
import gzip
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "bin/omarchy-mirror-update-packages"
API = runpy.run_path(str(SCRIPT))
TOOLS = ("repo-add", "pacman", "vercmp", "rclone", "gpg", "gpgv")


def command(argv, **kwargs):
    return subprocess.run([str(arg) for arg in argv], check=True, capture_output=True, **kwargs)


def tree(path):
    return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()}


def members(path):
    with tarfile.open(path) as archive:
        return {item.name: archive.extractfile(item).read() for item in archive if item.isfile()}


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "needs Arch repository tools, GnuPG and rclone")
class UpdatePackagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = Path(tempfile.mkdtemp())
        cls.gnupg = cls.shared / "gnupg"
        cls.gnupg.mkdir(mode=0o700)
        command(["gpg", "--homedir", cls.gnupg, "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                 "--quick-generate-key", "Mirror test <mirror@example.invalid>", "ed25519", "sign", "0"])
        cls.keyring = cls.shared / "keyring.gpg"
        cls.keyring.write_bytes(command(["gpg", "--homedir", cls.gnupg, "--export"]).stdout)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.gnupg), "--kill", "gpg-agent"], capture_output=True)
        shutil.rmtree(cls.shared)

    @classmethod
    def package(cls, name, version, dependencies=(), architecture="x86_64"):
        key = hashlib.sha256(repr((name, version, dependencies, architecture)).encode()).hexdigest()
        folder = cls.shared / key
        folder.mkdir(exist_ok=True)
        filename = folder / f"{name}-{version}-{architecture}.pkg.tar.xz"
        if not filename.exists():
            pkginfo = (f"pkgname = {name}\npkgbase = {name}\npkgver = {version}\npkgdesc = Test\n"
                       f"url = https://example.invalid\nbuilddate = 1\npackager = Test\nsize = 1\n"
                       f"arch = {architecture}\nlicense = MIT\n")
            pkginfo += "".join(f"depend = {dependency}\n" for dependency in dependencies)
            with tarfile.open(filename, "w:xz") as archive:
                for member_name, data in ((".PKGINFO", pkginfo.encode()), (f"usr/share/{name}/value", version.encode())):
                    member = tarfile.TarInfo(member_name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            command(["gpg", "--homedir", cls.gnupg, "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                     "--detach-sign", filename])
        return filename

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.run_number = 0
        self.core = [self.package("demo", "1-1"), self.package("helper", "1-1"), self.package("unchanged", "1-1")]
        self.source_core = [self.package("demo", "2-1"), self.package("helper", "2-1"), self.package("unchanged", "2-1")]
        for label, packages in (("source", self.source_core), ("rc", self.core), ("stable", self.core)):
            for repo in ("core", "extra", "multilib"):
                selected = packages if repo == "core" else [self.package(f"{repo}-filler", "1-1")]
                self.repository(self.root / label / repo / "os/x86_64", repo, selected)
            (self.root / label / "lastsync").write_text("100\n" if label == "source" else "42\n")
        self.config = self.root / "rclone.conf"
        self.config.touch()
        self.env = {**os.environ, "RCLONE_CONFIG": str(self.config), "RCLONE_CONFIG_OMARCHY_TYPE": "local"}

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(self.root)))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def repository(self, folder, repo, packages):
        folder.mkdir(parents=True, exist_ok=True)
        for package in packages:
            for source in (package, package.with_name(package.name + ".sig")):
                shutil.copyfile(source, folder / source.name)
        command(["repo-add", "--quiet", "--include-sigs", folder / f"{repo}.db.tar.gz",
                 *[folder / package.name for package in packages]])
        # R2 stores the database aliases as regular objects, not filesystem
        # symlinks. Match that when the real local rclone stands in for R2.
        for kind in ("db", "files"):
            alias = folder / f"{repo}.{kind}"
            data = alias.read_bytes()
            alias.unlink()
            alias.write_bytes(data)

    def update(self, packages=("demo",), apply=True, stable=True, architecture="x86_64", prefix=""):
        self.run_number += 1
        work = self.root / f"work-{self.run_number}"
        base = self.url + ("/" + prefix if prefix else "")
        destination = self.root / prefix
        args = ["python3", SCRIPT, "--architecture", architecture,
                "--upstream", base + "/source", "--keyring", self.keyring,
                "--work", work, "--rc-url", base + "/rc", "--stable-url", base + "/stable",
                "--rc-remote", f"omarchy:{destination}/rc", "--stable-remote", f"omarchy:{destination}/stable"]
        if apply:
            args.append("--apply")
        if stable:
            args.append("--promote-stable")
        result = subprocess.run([str(arg) for arg in [*args, "--", *packages]], capture_output=True, text=True, env=self.env)
        return result, json.loads((work / "plan.json").read_text())

    def test_only_selected_records_and_files_change_in_both_channels(self):
        # Identical records can have different archive timestamps per channel.
        repo = self.root / "rc/extra/os/x86_64"
        data = gzip.compress(gzip.decompress((repo / "extra.db").read_bytes()), mtime=1)
        for alias in ("extra.db", "extra.db.tar.gz"):
            (repo / alias).write_bytes(data)
        original = {channel: tree(self.root / channel) for channel in ("rc", "stable")}
        previous = {channel: API["database"](files["core/os/x86_64/core.db"])
                    for channel, files in original.items()}
        previous_files = {channel: members(self.root / channel / "core/os/x86_64/core.files")
                          for channel in original}
        result, plan = self.update(packages=("demo=2-1",))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["published"], ["rc", "stable"])
        for channel in ("rc", "stable"):
            folder = self.root / channel
            current = API["database"]((folder / "core/os/x86_64/core.db").read_bytes())
            self.assertEqual(current["demo"]["%VERSION%"], ["2-1"])
            for name in ("helper", "unchanged"):
                self.assertEqual(current[name], previous[channel][name])
            current_files = members(folder / "core/os/x86_64/core.files")
            for name, data in previous_files[channel].items():
                if not name.startswith("demo-"):
                    self.assertEqual(current_files[name], data)
            self.assertEqual((folder / "lastsync").read_text(), "42\n")
            floor = json.loads((folder / "package-update-floor.json").read_text())
            self.assertEqual(floor["packages"]["demo"]["version"], "2-1")
            self.assertEqual((folder / "core/os/x86_64/demo-2-1-x86_64.pkg.tar.xz.sig").read_bytes(),
                             self.package("demo", "2-1").with_name("demo-2-1-x86_64.pkg.tar.xz.sig").read_bytes())
            current_tree = tree(folder)
            for name, data in original[channel].items():
                if not name.startswith("core/os/x86_64/core."):
                    self.assertEqual(current_tree[name], data, f"{channel}/{name}")

    def test_read_only_plan_changes_neither_channel(self):
        before = {channel: tree(self.root / channel) for channel in ("rc", "stable")}
        result, plan = self.update(apply=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["published"], [])
        self.assertEqual(before, {channel: tree(self.root / channel) for channel in ("rc", "stable")})

    def test_rc_only_does_not_touch_stable(self):
        before = tree(self.root / "stable")
        result, plan = self.update(stable=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["published"], ["rc"])
        self.assertEqual(tree(self.root / "stable"), before)

    def test_arm_layout_uses_the_same_selected_package_flow(self):
        for channel in ("source", "rc", "stable"):
            for repo in ("core", "extra", "alarm", "aur"):
                name = "demo" if repo == "core" else f"{repo}-filler"
                version = "2-1" if channel == "source" and repo == "core" else "1-1"
                package = self.package(name, version, architecture="aarch64")
                self.repository(self.root / "arm" / channel / "aarch64" / repo, repo, [package])
            (self.root / "arm" / channel / "lastsync").write_text("100\n" if channel == "source" else "42\n")
        result, plan = self.update(architecture="aarch64", prefix="arm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["architecture"], "aarch64")
        self.assertEqual(plan["published"], ["rc", "stable"])
        current = API["database"]((self.root / "arm/stable/aarch64/core/core.db").read_bytes())
        self.assertEqual(current["demo"]["%VERSION%"], ["2-1"])

    def test_rerun_preserves_already_updated_packages(self):
        result, _ = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        before = {channel: tree(self.root / channel) for channel in ("rc", "stable")}
        result, plan = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(not item["updates"] for item in plan["channels"]))
        self.assertEqual(before, {channel: tree(self.root / channel) for channel in ("rc", "stable")})

    def test_missing_selected_dependency_stops_before_any_publication(self):
        needs_helper = self.package("demo", "2-1", ("helper>=2",))
        self.repository(self.root / "source/core/os/x86_64", "core", [needs_helper])
        before = {channel: tree(self.root / channel) for channel in ("rc", "stable")}
        result, plan = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pacman failed", result.stderr)
        self.assertEqual(plan["published"], [])
        self.assertEqual(before, {channel: tree(self.root / channel) for channel in ("rc", "stable")})

    def test_explicitly_selected_dependency_can_be_updated_together(self):
        self.repository(self.root / "source/core/os/x86_64", "core", [self.package("demo", "2-1", ("helper>=2",))])
        result, plan = self.update(packages=("demo", "helper"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["published"], ["rc", "stable"])

    def test_explicitly_selected_new_dependency_can_be_added(self):
        self.repository(self.root / "source/core/os/x86_64", "core", [
            self.package("demo", "2-1", ("new-helper>=2",)), self.package("new-helper", "2-1")])
        result, plan = self.update(packages=("demo", "new-helper"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["published"], ["rc", "stable"])

    def test_version_pin_must_match_upstream(self):
        result, plan = self.update(packages=("demo=9-1",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source serves 2-1", result.stderr)
        self.assertEqual(plan["published"], [])

    def test_invalid_or_duplicate_package_selection_is_rejected(self):
        for packages in (("demo", "demo"), ("demo; touch pwned",), ("--malicious",)):
            result, plan = self.update(packages=packages)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid or duplicate package selection", result.stderr)
            self.assertEqual(plan["published"], [])

    def test_refuses_to_downgrade_either_destination(self):
        self.repository(self.root / "stable/core/os/x86_64", "core", [self.package("demo", "3-1")])
        before = tree(self.root / "rc")
        result, plan = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to downgrade stable/demo", result.stderr)
        self.assertEqual(plan["published"], [])
        self.assertEqual(tree(self.root / "rc"), before)

    def test_existing_protected_version_is_checked_before_any_publication(self):
        floor = {"schema": 1, "architecture": "x86_64", "packages": {
            "demo": {"version": "3-1", "sha256": hashlib.sha256(self.package("demo", "3-1").read_bytes()).hexdigest()}}}
        (self.root / "stable/package-update-floor.json").write_text(json.dumps(floor))
        before = tree(self.root / "rc")
        result, plan = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("older than targeted package updates", result.stderr)
        self.assertEqual(plan["published"], [])
        self.assertEqual(tree(self.root / "rc"), before)

    def test_refuses_signed_destination_metadata(self):
        (self.root / "stable/core/os/x86_64/core.db.sig").write_bytes(b"retain this signature")
        before = tree(self.root / "rc")
        result, plan = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signed databases", result.stderr)
        self.assertEqual(plan["published"], [])
        self.assertEqual(tree(self.root / "rc"), before)

    def test_bad_package_checksum_stops_before_publication(self):
        (self.root / "source/core/os/x86_64/demo-2-1-x86_64.pkg.tar.xz").write_bytes(b"corrupt package")
        result, plan = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum/size mismatch", result.stderr)
        self.assertEqual(plan["published"], [])

    def test_bad_package_signature_stops_before_publication(self):
        archive = self.root / "source/core/os/x86_64/core.db.tar.gz"
        data = members(archive)
        filename = next(name for name in data if name.startswith("demo-") and name.endswith("/desc"))
        original = data[filename].decode()
        signature = original.split("%PGPSIG%\n")[1].split("\n")[0]
        data[filename] = original.replace(signature, base64.b64encode(b"bad signature").decode()).encode()
        with tarfile.open(archive, "w:gz") as output:
            for name, content in data.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                output.addfile(member, io.BytesIO(content))
        (archive.parent / "core.db").write_bytes(archive.read_bytes())
        result, plan = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gpgv failed", result.stderr)
        self.assertEqual(plan["published"], [])

    def test_failed_rc_readback_prevents_stable_publication(self):
        wrappers = self.root / "bin"
        wrappers.mkdir()
        marker = self.root / "rc-published"
        remote = f"omarchy:{self.root}/rc/core/os/x86_64/core.db"
        wrapper = wrappers / "rclone"
        wrapper.write_text(
            '#!/bin/bash\nset -euo pipefail\n'
            f'if [[ $1 == "cat" && $2 == "{remote}" && -f "{marker}" ]]; then\n'
            '  echo "corrupt readback"\n  exit 0\nfi\n'
            f'"{shutil.which("rclone")}" "$@"\n'
            f'if [[ $1 == "copyto" && $3 == "{remote}" ]]; then touch "{marker}"; fi\n'
        )
        wrapper.chmod(0o755)
        self.env["PATH"] = f"{wrappers}:{os.environ['PATH']}"
        before = tree(self.root / "stable")
        result, plan = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Published database verification failed: rc", result.stderr)
        self.assertEqual(plan["published"], [])
        self.assertEqual(tree(self.root / "stable"), before)

    def test_intervening_metadata_change_is_not_overwritten(self):
        wrappers = self.root / "bin"
        wrappers.mkdir()
        database_file = self.root / "rc/core/os/x86_64/core.db"
        original = database_file.read_bytes()
        remote = f"omarchy:{self.root}/rc/core/os/x86_64/demo-2-1-x86_64.pkg.tar.xz"
        wrapper = wrappers / "rclone"
        wrapper.write_text(
            '#!/bin/bash\nset -euo pipefail\n'
            f'"{shutil.which("rclone")}" "$@"\n'
            f'if [[ $1 == "copyto" && $3 == "{remote}" ]]; then\n'
            f'  printf "changed" >> "{database_file}"\nfi\n'
        )
        wrapper.chmod(0o755)
        self.env["PATH"] = f"{wrappers}:{os.environ['PATH']}"
        stable = tree(self.root / "stable")
        result, plan = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed during preparation", result.stderr)
        self.assertEqual(database_file.read_bytes(), original + b"changed")
        self.assertEqual(tree(self.root / "stable"), stable)
        self.assertEqual(plan["published"], [])


if __name__ == "__main__":
    unittest.main()
