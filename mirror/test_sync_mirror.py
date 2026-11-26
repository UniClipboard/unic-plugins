#!/usr/bin/env python3
"""Unit and integration tests for mirror/sync_mirror.py."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

import sync_mirror


def _build_tar_gz(files: dict[str, bytes], with_pax: bool = True) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if with_pax:
            pax = tarfile.TarInfo("pax_global_header")
            pax.type = tarfile.XGLTYPE
            pax.pax_headers = {"comment": "synthetic GitHub source archive"}
            tf.addfile(pax, io.BytesIO(b""))
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class SyncMirrorTests(unittest.TestCase):
    def test_python310_fallback_toml_parser(self) -> None:
        spec = """
manifest_version = 1
id = "unic.demo"
version = "0.1.0"
kind = "go"

[[artifacts]]
goos = "darwin" # comment
goarch = "arm64"
release = "v0.1.0"
asset = "demo_darwin_arm64.tar.gz"
executable = "demo"
sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[[artifacts]]
goos = 'linux'
goarch = 'amd64'
release = 'v0.1.0'
asset = 'demo_linux_amd64.tar.gz'
executable = 'demo'
sha256 = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'

[i18n.en]
name = "Demo"
"""
        arts = sync_mirror.parse_spec_artifacts_fallback(spec)
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["asset"], "demo_darwin_arm64.tar.gz")
        self.assertEqual(arts[1]["goos"], "linux")
        self.assertEqual(arts[1]["sha256"], "b" * 64)

    def test_end_to_end_sync_etag_prune_and_digest_rejection(self) -> None:
        good_commit = "a" * 40
        tag_commit = "b" * 40
        asset_bytes = _build_tar_gz({"demo": b"binary-content"}, with_pax=False)
        asset_sha = hashlib.sha256(asset_bytes).hexdigest()

        spec_good = f"""
manifest_version = 1
id = "unic.demo"
version = "0.1.0"
api_version = 1
kind = "go"
module = "example.com/demo"
package = "."
capabilities = ["action"]

[[artifacts]]
goos = "darwin"
goarch = "arm64"
release = "v0.1.0"
asset = "demo_darwin_arm64.tar.gz"
executable = "demo"
sha256 = "{asset_sha}"
""".encode("utf-8")

        source_good = _build_tar_gz({"demo-aaaa/spec.toml": spec_good})
        source_tag = _build_tar_gz({"demo-bbbb/plugins/sub/spec.toml": spec_good})

        registry = {
            "version": 1,
            "updated_at": "2026-11-26",
            "plugins": [
                {
                    "id": "unic.demo",
                    "repository": "https://github.com/UniClipboard/unic-plugin-demo",
                    "ref": good_commit,
                },
                {
                    "id": "unic.tagged",
                    "repository": "https://github.com/UniClipboard/unic-plugin-tagged",
                    "ref": "v0.1.0",
                    "path": "plugins/sub",
                },
            ],
        }
        registry_bytes = json.dumps(registry).encode("utf-8")

        state = {
            "corrupt_asset": False,
            "etag": '"v1"',
            "hits": [],
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_GET(self):  # noqa: N802
                state["hits"].append(self.path)
                if self.path == "/registry.json":
                    if self.headers.get("If-None-Match") == state["etag"]:
                        self.send_response(304)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("ETag", state["etag"])
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(registry_bytes)
                    return
                if self.path == "/api/repos/uniclipboard/unic-plugin-tagged/commits/v0.1.0":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"sha": tag_commit}).encode("utf-8"))
                    return
                if self.path == f"/codeload/uniclipboard/unic-plugin-demo/tar.gz/{good_commit}":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(source_good)
                    return
                if self.path == f"/codeload/uniclipboard/unic-plugin-tagged/tar.gz/{tag_commit}":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(source_tag)
                    return
                if self.path.endswith("/releases/download/v0.1.0/demo_darwin_arm64.tar.gz"):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"corrupted" if state["corrupt_asset"] else asset_bytes)
                    return
                self.send_response(404)
                self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        base = f"http://127.0.0.1:{srv.server_port}"
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "mirror"
                # 1. Corrupt asset must fail and NOT publish registry.json.
                state["corrupt_asset"] = True
                with self.assertRaises(sync_mirror.MirrorError):
                    sync_mirror.sync_mirror(
                        out,
                        registry_url=f"{base}/registry.json",
                        api_base=f"{base}/api",
                        codeload_base=f"{base}/codeload",
                        github_base=f"{base}/github",
                        allow_http_loopback=True,
                    )
                self.assertFalse((out / "registry.json").exists())

                # 2. Valid sync publishes all expected files and skips API for 40-char SHA.
                state["corrupt_asset"] = False
                state["hits"].clear()
                res = sync_mirror.sync_mirror(
                    out,
                    registry_url=f"{base}/registry.json",
                    api_base=f"{base}/api",
                    codeload_base=f"{base}/codeload",
                    github_base=f"{base}/github",
                    allow_http_loopback=True,
                )
                self.assertEqual(res["status"], "synced")
                self.assertEqual(res["plugins"], 2)
                self.assertEqual(res["verified_assets"], 2)
                self.assertNotIn(
                    f"/api/repos/uniclipboard/unic-plugin-demo/commits/{good_commit}",
                    state["hits"],
                )
                self.assertTrue((out / "registry.json").exists())
                self.assertTrue((out / "health.json").exists())
                self.assertTrue(
                    (
                        out
                        / "api.github.com/repos/uniclipboard/unic-plugin-demo/commits"
                        / good_commit
                    ).exists()
                )
                self.assertTrue(
                    (
                        out
                        / "github.com/uniclipboard/unic-plugin-demo/releases/download/v0.1.0/demo_darwin_arm64.tar.gz"
                    ).exists()
                )

                # 3. Second sync hits ETag 304 and skips work.
                res2 = sync_mirror.sync_mirror(
                    out,
                    registry_url=f"{base}/registry.json",
                    api_base=f"{base}/api",
                    codeload_base=f"{base}/codeload",
                    github_base=f"{base}/github",
                    allow_http_loopback=True,
                )
                self.assertEqual(res2["status"], "not_modified")

                # 4. Stale file is cleaned up with --prune --force.
                stale = out / "codeload.github.com/old/repo/tar.gz" / ("c" * 40)
                stale.parent.mkdir(parents=True, exist_ok=True)
                stale.write_bytes(b"stale")
                res3 = sync_mirror.sync_mirror(
                    out,
                    registry_url=f"{base}/registry.json",
                    api_base=f"{base}/api",
                    codeload_base=f"{base}/codeload",
                    github_base=f"{base}/github",
                    force=True,
                    prune=True,
                    allow_http_loopback=True,
                )
                self.assertEqual(res3["pruned"], 1)
                self.assertFalse(stale.exists())
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
