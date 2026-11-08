from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse

from test_promote_arm import rclone_env


BIN = Path(__file__).resolve().parents[1] / "bin"
PURGE = BIN / "omarchy-mirror-purge"
PKG = "aarch64/extra/python-protobuf-36.1-1-aarch64.pkg.tar.xz"


class Edge:
    """A Cloudflare stand-in: a cache in front of a bucket directory that keeps
    whatever it served first, and the purge API that drops it. stale_purge
    makes the API accept a purge without dropping anything."""

    def __init__(self, test, bucket):
        self.bucket, self.cached, self.purged, self.refuse = bucket, {}, [], 0
        self.stale_purge, self.on_purge = False, None
        edge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urllib.parse.unquote(self.path.split("?")[0]).lstrip("/")
                if path not in edge.cached:
                    source = edge.bucket / path
                    if not source.is_file():
                        self.send_error(404)
                        return
                    edge.cached[path] = source.read_bytes()
                body = edge.cached[path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("Authorization") != "Bearer test-token":
                    answer = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
                elif edge.refuse:
                    edge.refuse -= 1
                    answer = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
                else:
                    edge.purged.append((self.path, body["files"]))
                    if edge.on_purge:
                        edge.on_purge()
                    if not edge.stale_purge:
                        for url in body["files"]:
                            edge.cached.pop(urllib.parse.unquote(urllib.parse.urlsplit(url).path).lstrip("/"), None)
                    answer = {"success": True, "result": {"id": "zone"}}
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        test.addCleanup(server.server_close)
        test.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"

    def urls(self):
        return [url for _, files in self.purged for url in files]


# The real rclone over local directories standing in for the buckets, as in
# test_promote_arm: the listing it gives is what the purge decides by.
@unittest.skipUnless(shutil.which("rclone"), "needs rclone")
class PurgeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.bucket = self.root / "bucket"
        self.write(PKG, b"old build " * 47092)
        self.write(PKG + ".sig", b"old signature")
        self.write("aarch64/extra/extra.db", b"old database")
        self.edge = Edge(self, self.bucket)
        self.env = {**rclone_env(self.root), "OMARCHY_MIRROR_STATE": str(self.root / "state"),
                    "CLOUDFLARE_API": self.edge.url, "CLOUDFLARE_ZONE_ID": "zone",
                    "CLOUDFLARE_PURGE_TOKEN": "test-token",
                    "CLOUDFLARE_PURGE_ENV": str(self.root / "no-such.env")}

    def write(self, key, data):
        path = self.bucket / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        # A rewrite gets a newer time than the first write, as on R2.
        stamp = time.time() + len(list(self.root.rglob("*")))
        os.utime(path, (stamp, stamp))

    def run_purge(self, step, **env):
        return subprocess.run(
            [str(PURGE), step, "--r2", f"omarchy:{self.bucket}", "--url", self.edge.url, "--wait", "0"],
            capture_output=True, text=True, env={**self.env, **env}, timeout=120,
        )

    def baseline(self):
        return list((self.root / "state/purge").glob("*.json"))

    def test_a_replaced_package_is_purged_and_then_served_new(self):
        self.edge.cached[PKG] = (self.bucket / PKG).read_bytes()
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write(PKG, b"rebuilt " * 58857)

        result = self.run_purge("finish")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.edge.urls(), [f"{self.edge.url}/{PKG}"])
        self.assertEqual(self.edge.cached[PKG], b"rebuilt " * 58857)
        self.assertEqual(self.baseline(), [])

    def test_nothing_replaced_needs_no_token_and_no_purge(self):
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write("aarch64/extra/new-1-1-aarch64.pkg.tar.xz", b"a new package")
        self.write("aarch64/extra/extra.db", b"a new database")

        result = self.run_purge("finish", CLOUDFLARE_PURGE_TOKEN="")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.edge.purged, [])
        self.assertEqual(self.baseline(), [])

    def test_a_replacement_without_a_token_holds_the_databases(self):
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write(PKG + ".sig", b"new signature")

        result = self.run_purge("finish", CLOUDFLARE_PURGE_TOKEN="")

        self.assertEqual(result.returncode, 1)
        self.assertIn("CLOUDFLARE_PURGE_TOKEN", result.stderr)
        self.assertEqual(len(self.baseline()), 1, "the list stays for the next run")

    def test_a_purge_that_does_not_take_fails_and_the_next_run_purges_it(self):
        self.edge.cached[PKG] = (self.bucket / PKG).read_bytes()
        self.edge.stale_purge = True
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write(PKG, b"rebuilt " * 58857)

        failed = self.run_purge("finish")

        self.assertEqual(failed.returncode, 1)
        self.assertIn(PKG, failed.stderr)
        self.assertEqual(len(self.edge.purged), 3, "one purge for each attempt")

        # The next run starts from the old list, so the replacement is still found.
        self.edge.stale_purge = False
        self.assertEqual(self.run_purge("begin").returncode, 0)
        result = self.run_purge("finish")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.edge.cached[PKG], b"rebuilt " * 58857)

    def test_an_api_error_is_retried(self):
        self.edge.refuse = 1  # a new token is refused for a few seconds
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write(PKG, b"rebuilt " * 58857)

        result = self.run_purge("finish")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.edge.purged), 1)

    def test_an_epoch_name_is_purged_as_pacman_asks_for_it_and_encoded(self):
        key = "aarch64/core/libnet-2:1.3-2-aarch64.pkg.tar.xz"
        self.write(key, b"libnet one")
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write(key, b"libnet two")

        result = self.run_purge("finish")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(self.edge.urls()), sorted([
            f"{self.edge.url}/aarch64/core/libnet-2:1.3-2-aarch64.pkg.tar.xz",
            f"{self.edge.url}/aarch64/core/libnet-2%3A1.3-2-aarch64.pkg.tar.xz",
        ]))

    def test_the_token_is_read_from_the_env_file(self):
        env_file = self.root / "cloudflare.env"
        env_file.write_text("CLOUDFLARE_PURGE_TOKEN=test-token\n")
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write(PKG, b"rebuilt " * 58857)
        env = {k: v for k, v in self.env.items() if k != "CLOUDFLARE_PURGE_TOKEN"}
        env["CLOUDFLARE_PURGE_ENV"] = str(env_file)

        result = subprocess.run(
            [str(PURGE), "finish", "--r2", f"omarchy:{self.bucket}", "--url", self.edge.url, "--wait", "0"],
            capture_output=True, text=True, env=env, timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.edge.purged), 1)

    def test_a_resumed_run_still_catches_a_package_the_failed_run_added(self):
        new = "aarch64/extra/new-1-1-aarch64.pkg.tar.xz"
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write(new, b"first build")
        self.edge.cached[new] = b"first build"
        # The run fails here, before finish. The next one begins again.
        self.assertEqual(self.run_purge("begin").returncode, 0)
        self.write(new, b"second build")

        result = self.run_purge("finish")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.edge.urls(), [f"{self.edge.url}/{new}"])
        self.assertEqual(self.edge.cached[new], b"second build")

    def test_finish_without_begin_fails(self):
        result = self.run_purge("finish")

        self.assertEqual(result.returncode, 1)
        self.assertIn("run begin first", result.stderr)


@unittest.skipUnless(shutil.which("rclone"), "needs rclone")
class PromotePurgeTest(unittest.TestCase):
    """omarchy-mirror-promote-arm copies server-side, and rclone logs a new and
    a replaced object alike there: the purge must still find the rebuild, and
    purge it before the destination's database moves."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def bucket(self, name, package, database, lastsync):
        path = self.root / name
        (path / "aarch64/extra").mkdir(parents=True)
        (path / PKG).write_bytes(package)
        (path / "aarch64/extra/extra.db").write_bytes(database)
        (path / "lastsync").write_text(f"{lastsync}\n")
        return path

    def test_promote_purges_a_rebuild_before_the_database(self):
        edge = self.bucket("edge", b"rebuilt " * 58857, b"edge database", 1794175436)
        old = os.path.getmtime(edge / PKG) - 86400
        rc = self.bucket("rc", b"old build " * 47092, b"rc database", 1792519411)
        os.utime(rc / PKG, (old, old))
        cdn = Edge(self, rc)
        cdn.cached[PKG] = (rc / PKG).read_bytes()
        seen = []
        cdn.on_purge = lambda: seen.append((rc / "aarch64/extra/extra.db").read_bytes())
        urls = self.root / "public-urls"
        urls.write_text(f"omarchy:{rc} {cdn.url}\n")
        env = {**rclone_env(self.root), "OMARCHY_MIRROR_STATE": str(self.root / "state"),
               "OMARCHY_MIRROR_PUBLIC_URLS": str(urls), "CLOUDFLARE_API": cdn.url,
               "CLOUDFLARE_ZONE_ID": "zone", "CLOUDFLARE_PURGE_TOKEN": "test-token"}

        result = subprocess.run(
            [str(BIN / "omarchy-mirror-promote-arm"), "--from", f"omarchy:{edge}", "--to", f"omarchy:{rc}"],
            capture_output=True, text=True, env=env, timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(cdn.urls(), [f"{cdn.url}/{PKG}"])
        self.assertEqual(seen, [b"rc database"], "the purge runs before the database moves")
        self.assertEqual(cdn.cached[PKG], b"rebuilt " * 58857)
        self.assertEqual((rc / "aarch64/extra/extra.db").read_bytes(), b"edge database")

    def test_promote_purges_what_the_prune_writes(self):
        # The source rebuilds the package, and its database, while the promote
        # runs: the prune, which copies by checksum after the databases, writes
        # the package again. The new database must not go up before its purge.
        edge = self.bucket("edge", b"rebuilt " * 58857, b"edge database", 1794175436)
        rc = self.bucket("rc", b"rebuilt " * 58857, b"rc database", 1792519411)
        cdn = Edge(self, rc)
        urls = self.root / "public-urls"
        urls.write_text(f"omarchy:{rc} {cdn.url}\n")
        wrapper = self.root / "bin/rclone"
        wrapper.parent.mkdir()
        marker = self.root / "rebuilt"
        wrapper.write_text(
            "#!/bin/bash\n"
            f'if [[ $1 == sync && ! -e {marker} ]]; then\n'
            f'  printf "rebuilt again" >{edge / PKG}; printf "edge database 2" >{edge}/aarch64/extra/extra.db\n'
            f'  touch {marker}\n'
            "fi\n"
            f'PATH="{os.environ["PATH"]}" exec rclone "$@"\n'
        )
        wrapper.chmod(0o755)
        env = {**rclone_env(self.root), "PATH": f"{wrapper.parent}:{os.environ['PATH']}",
               "OMARCHY_MIRROR_STATE": str(self.root / "state"), "OMARCHY_MIRROR_PUBLIC_URLS": str(urls),
               "CLOUDFLARE_API": cdn.url, "CLOUDFLARE_ZONE_ID": "zone", "CLOUDFLARE_PURGE_TOKEN": "test-token"}
        cdn.cached[PKG] = (rc / PKG).read_bytes()
        seen = []
        cdn.on_purge = lambda: seen.append((rc / "aarch64/extra/extra.db").read_bytes())

        result = subprocess.run(
            [str(BIN / "omarchy-mirror-promote-arm"), "--from", f"omarchy:{edge}", "--to", f"omarchy:{rc}"],
            capture_output=True, text=True, env=env, timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((rc / PKG).read_bytes(), b"rebuilt again")
        self.assertEqual(cdn.cached.get(PKG, b"rebuilt again"), b"rebuilt again")
        self.assertIn(f"{cdn.url}/{PKG}", cdn.urls())
        self.assertEqual(seen[0], b"edge database", "the purge comes before the new database")
        self.assertEqual((rc / "aarch64/extra/extra.db").read_bytes(), b"edge database 2")


if __name__ == "__main__":
    unittest.main()
