import importlib.machinery
import importlib.util
import io
import itertools
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin"
loader = importlib.machinery.SourceFileLoader("backwards", str(BIN / "omarchy-mirror-backwards"))
spec = importlib.util.spec_from_loader("backwards", loader)
backwards = importlib.util.module_from_spec(spec)
loader.exec_module(backwards)

# Orderings from pacman's vercmp(8) and libalpm's own tests, oldest first.
ORDERED = [
    ["1.0a", "1.0b", "1.0beta", "1.0p", "1.0pre", "1.0rc", "1.0", "1.0.a", "1.0.1"],
    ["1", "1.0", "1.1", "1.1.1", "1.2", "2.0", "3.0.0"],
    ["1.0-1", "1.0-2", "1.0.1-1", "1:0.1-1", "1:1.0-1", "2:0.1-1"],
    ["155.0.8059.84-1", "157.0.8103.38-1", "157.0.8103.78-1"],
    ["158.0.1-1", "160.0-1"],
    ["1.2.0.2-1", "1.2.1.0-1"],
    ["2.44+r50+g1848099f063e-1", "2.44+r51+g0000000-1"],
]
EQUAL = [("1.0", "1.0"), ("1.0-1", "1.0"), ("1.0", "1.0-2"), ("0:1.0", "1.0"), (":1.0", "1.0"), ("1.001", "1.1")]


class VercmpTest(unittest.TestCase):
    def test_orders_versions_as_pacman_does(self):
        for chain in ORDERED:
            for older, newer in itertools.combinations(chain, 2):
                with self.subTest(older=older, newer=newer):
                    self.assertEqual(backwards.vercmp(older, newer), -1)
                    self.assertEqual(backwards.vercmp(newer, older), 1)

    def test_equal_versions(self):
        for a, b in EQUAL:
            with self.subTest(a=a, b=b):
                self.assertEqual(backwards.vercmp(a, b), 0)

    @unittest.skipUnless(shutil.which("vercmp"), "pacman's vercmp is not installed")
    def test_agrees_with_pacman(self):
        versions = sorted({v for chain in ORDERED for v in chain} | {v for pair in EQUAL for v in pair} | {
            "1.0~rc1", "1.0_1", "1.0+1", "1.0.0a", "a", "1a", "1.a", "1..1", "1.1.", "1.0..0", "1.0.0", "20260102", "r123.abc", "0.9.9.r12.g3e4f",
        })
        for a, b in itertools.product(versions, repeat=2):
            expected = int(subprocess.run(["vercmp", a, b], capture_output=True, text=True, check=True).stdout)
            with self.subTest(a=a, b=b):
                self.assertEqual(backwards.vercmp(a, b), expected)


class BackwardsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def stage(self, name, repos):
        for repo, packages in repos.items():
            path = self.root / name / repo / "os/x86_64"
            path.mkdir(parents=True)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                for package, version in packages.items():
                    data = f"%NAME%\n{package}\n\n%VERSION%\n{version}\n\n".encode()
                    info = tarfile.TarInfo(f"{package}-{version}/desc")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            (path / f"{repo}.db").write_bytes(buffer.getvalue())
        return self.root / name

    def run_tool(self, source, target):
        return subprocess.run([str(BIN / "omarchy-mirror-backwards"), str(source), str(target)], capture_output=True, text=True)

    def test_lists_only_packages_that_would_move_back(self):
        source = self.stage("source", {"core": {"brotli": "1.2.0-1", "glibc": "2.44-1"}, "extra": {"firefox": "160.0-1", "new": "1-1"}})
        target = self.stage("target", {"core": {"brotli": "1.2.1-1", "glibc": "2.43-1", "gone": "1-1"}, "extra": {"firefox": "160.0-1"}})

        result = self.run_tool(source, target)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(), ["core brotli 1.2.1-1 -> 1.2.0-1"])

    def test_follows_a_package_into_another_repository(self):
        source = self.stage("source", {"core": {"foo": "1-1"}, "extra": {}})
        target = self.stage("target", {"core": {}, "extra": {"foo": "2-1"}})

        result = self.run_tool(source, target)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(), ["extra foo 2-1 -> 1-1 (in core)"])

    def test_leaves_out_repositories_machines_do_not_use(self):
        source = self.stage("source", {"core": {"brotli": "1.2.1-1"}, "core-testing": {"glibc": "2.44-1"}})
        target = self.stage("target", {"core": {"brotli": "1.2.0-1"}, "core-testing": {"glibc": "2.45-1"}})

        result = self.run_tool(source, target)

        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_nothing_moves_back(self):
        source = self.stage("source", {"core": {"brotli": "1.2.1-1"}})
        target = self.stage("target", {"core": {"brotli": "1.2.0-1"}, "extra": {"firefox": "160.0-1"}})

        result = self.run_tool(source, target)

        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_a_database_it_cannot_read_is_an_error(self):
        source = self.stage("source", {"core": {"brotli": "1.2.1-1"}})
        target = self.stage("target", {"core": {"brotli": "1.2.0-1"}})
        (source / "core/os/x86_64/core.db").write_text("not a database\n")

        result = self.run_tool(source, target)

        self.assertEqual(result.returncode, 2)
        self.assertIn("can't read", result.stderr)


if __name__ == "__main__":
    unittest.main()
