#!/usr/bin/env python3
"""Static mirror synchronizer for UniClipboard's plugin marketplace.

Mirrors registry.json, commit resolution JSON, source tarballs, and SHA-256
verified GitHub Release assets into a static web root compatible with unic's
mirror layout:

  <output>/registry.json
  <output>/health.json
  <output>/api.github.com/repos/<owner>/<repo>/commits/<ref>
  <output>/codeload.github.com/<owner>/<repo>/tar.gz/<commit>
  <output>/github.com/<owner>/<repo>/releases/download/<release>/<asset>

Uses Python 3.10+ standard library only (no third-party packages required).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import posixpath
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/UniClipboard/unic-plugins/main/registry.json"
)
DEFAULT_MIRROR_DOMAIN = "plugins.uniclipboard.app"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
ARCHIVE_LIMIT = 128 * 1024 * 1024  # 128 MiB (matches unic installer)
REGISTRY_LIMIT = 512 * 1024  # 512 KiB (matches unic marketplace)
COMMIT_API_LIMIT = 1024 * 1024  # 1 MiB
REQUEST_TIMEOUT = 60

GITHUB_REPO_RE = re.compile(r"^/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)$")
COMMIT_SHA_RE = re.compile(r"^[a-f0-9]{40}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

ALLOWED_GITHUB_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
)


class MirrorError(RuntimeError):
    """Raised when mirror synchronization fails safely before publication."""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str], allow_http_loopback: bool = False):
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.allow_http_loopback = allow_http_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        host = (parsed.hostname or "").lower()
        is_loopback = host in {"127.0.0.1", "localhost", "::1"}
        if parsed.username or parsed.password:
            raise MirrorError(f"rejected redirect with credentials: {parsed.netloc}")
        if parsed.scheme != "https" and not (
            self.allow_http_loopback and parsed.scheme == "http" and is_loopback
        ):
            raise MirrorError(f"rejected non-HTTPS redirect: {parsed.scheme}://{host}")
        if host not in self.allowed_hosts and not (
            self.allow_http_loopback and is_loopback
        ):
            raise MirrorError(f"rejected redirect to unexpected host: {host}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(allow_http_loopback: bool = False) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _SafeRedirectHandler(ALLOWED_GITHUB_HOSTS, allow_http_loopback=allow_http_loopback)
    )


def _validate_url(raw: str, allow_http_loopback: bool = False) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not host or parsed.username or parsed.password:
        raise MirrorError(f"invalid URL: {raw}")
    if parsed.scheme != "https" and not (
        allow_http_loopback and parsed.scheme == "http" and is_loopback
    ):
        raise MirrorError(f"URL must use HTTPS: {raw}")
    return parsed


def _parse_github_slug(repo_url: str) -> str:
    parsed = urllib.parse.urlparse(repo_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise MirrorError(f"invalid GitHub repository URL: {repo_url}")
    if not GITHUB_REPO_RE.match(parsed.path):
        raise MirrorError(f"invalid GitHub repository path: {repo_url}")
    slug = parsed.path.lower()
    if slug.endswith(".git"):
        slug = slug[:-4]
    owner, name = slug.lstrip("/").split("/", 1)
    if name in {"", ".", ".."}:
        raise MirrorError(f"invalid GitHub repository name: {repo_url}")
    return f"/{owner}/{name}"


def _valid_local_path(p: str) -> bool:
    if not p:
        return True
    if p.startswith("/") or "\\" in p or "\x00" in p or ":" in p:
        return False
    parts = p.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _http_get_bytes(
    opener: urllib.request.OpenerDirector,
    url: str,
    limit: int,
    headers: dict[str, str] | None = None,
    allow_http_loopback: bool = False,
) -> tuple[int, bytes, dict[str, str]]:
    _validate_url(url, allow_http_loopback=allow_http_loopback)
    req_headers = {"User-Agent": "unic-plugins-mirror/1.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            cl = resp_headers.get("content-length")
            if cl is not None and cl.isdigit() and int(cl) > limit:
                raise MirrorError(f"response from {url} exceeds size limit ({cl} > {limit})")
            data = resp.read(limit + 1)
            if len(data) > limit:
                raise MirrorError(f"response from {url} exceeds size limit ({limit})")
            return status, data, resp_headers
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 304:
                return 304, b"", {k.lower(): v for k, v in exc.headers.items()}
            raise MirrorError(f"HTTP {exc.code} fetching {url}") from exc
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise MirrorError(f"network error fetching {url}: {exc.reason}") from exc


def _http_download_file(
    opener: urllib.request.OpenerDirector,
    url: str,
    dest: Path,
    limit: int,
    expected_sha256: str | None = None,
    allow_http_loopback: bool = False,
) -> int:
    _validate_url(url, allow_http_loopback=allow_http_loopback)
    req = urllib.request.Request(
        url, headers={"User-Agent": "unic-plugins-mirror/1.0"}, method="GET"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(dest.parent))
    total = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                    cl = resp.headers.get("Content-Length")
                    if cl is not None and cl.isdigit() and int(cl) > limit:
                        raise MirrorError(f"asset {url} exceeds limit ({cl} > {limit})")
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise MirrorError(f"asset {url} exceeds limit ({limit})")
                        digest.update(chunk)
                        out.write(chunk)
            except urllib.error.HTTPError as exc:
                raise MirrorError(f"HTTP {exc.code} downloading {url}") from exc
            except urllib.error.URLError as exc:
                raise MirrorError(f"network error downloading {url}: {exc.reason}") from exc
            out.flush()
            os.fsync(out.fileno())
        got_sha = digest.hexdigest()
        if expected_sha256 is not None and got_sha != expected_sha256:
            raise MirrorError(
                f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {got_sha}"
            )
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, dest)
        return total
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _strip_toml_comment(line: str) -> str:
    in_double = False
    in_single = False
    escaped = False
    out: list[str] = []
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if in_double:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            out.append(ch)
            continue
        if in_single:
            if ch == "'":
                in_single = False
            out.append(ch)
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
        elif ch == "'":
            in_single = True
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _parse_toml_string(val: str) -> str:
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return json.loads(val)
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    raise MirrorError(f"unsupported TOML string value: {val}")


def parse_spec_artifacts_fallback(spec_text: str) -> list[dict[str, str]]:
    """Minimal stdlib-only parser for [[artifacts]] in spec.toml (Python 3.10)."""
    artifacts: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in spec_text.splitlines():
        line = _strip_toml_comment(raw_line)
        if not line:
            continue
        if line == "[[artifacts]]":
            current = {}
            artifacts.append(current)
            continue
        if line.startswith("["):
            current = None
            continue
        if current is None or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key in {"goos", "goarch", "release", "asset", "executable", "sha256"}:
            current[key] = _parse_toml_string(val)
    return artifacts


def parse_spec_artifacts(spec_bytes: bytes) -> list[dict[str, str]]:
    """Extract [[artifacts]] entries from a plugin's spec.toml."""
    if tomllib is not None:
        doc = tomllib.loads(spec_bytes.decode("utf-8"))
        raw = doc.get("artifacts") or []
        out: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict):
                out.append({k: str(v) for k, v in item.items() if isinstance(v, str)})
        return out
    return parse_spec_artifacts_fallback(spec_bytes.decode("utf-8"))


def extract_spec_toml(archive_path: Path, subpath: str) -> bytes:
    """Read spec.toml from a GitHub codeload tar.gz archive (ignoring PAX global headers)."""
    target_rel = posixpath.join(subpath, "spec.toml") if subpath else "spec.toml"
    with tarfile.open(archive_path, mode="r:gz") as tf:
        archive_root: str | None = None
        for member in tf.getmembers():
            if member.type == tarfile.XGLTYPE:
                continue
            name = member.name.rstrip("/")
            if not name or "/" not in name:
                if archive_root is None and name and member.isdir():
                    archive_root = name
                continue
            first, rest = name.split("/", 1)
            if archive_root is None:
                archive_root = first
            if first != archive_root:
                raise MirrorError(f"unexpected archive root in {archive_path}")
            if rest == target_rel and member.isfile():
                fh = tf.extractfile(member)
                if fh is None:
                    raise MirrorError(f"unable to read {target_rel} from {archive_path}")
                data = fh.read(1024 * 1024)
                return data
    raise MirrorError(f"{target_rel} not found in {archive_path}")


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _prune_unreferenced(output_dir: Path, keep_files: set[Path]) -> int:
    removed = 0
    managed_roots = [
        output_dir / "api.github.com",
        output_dir / "codeload.github.com",
        output_dir / "github.com",
    ]
    resolved_keep = {p.resolve() for p in keep_files}
    for root_dir in managed_roots:
        if not root_dir.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir, topdown=False):
            dp = Path(dirpath)
            for fname in filenames:
                fp = dp / fname
                if fname.startswith(".tmp-"):
                    continue
                if fp.resolve() not in resolved_keep:
                    fp.unlink()
                    removed += 1
            for dname in dirnames:
                sub = dp / dname
                try:
                    sub.rmdir()
                except OSError:
                    pass
    return removed


def sync_mirror(
    output_dir: Path,
    *,
    registry_url: str = DEFAULT_REGISTRY_URL,
    registry_file: Path | None = None,
    mirror_domain: str = DEFAULT_MIRROR_DOMAIN,
    github_token: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
    force: bool = False,
    prune: bool = False,
    dry_run: bool = False,
    api_base: str = "https://api.github.com",
    codeload_base: str = "https://codeload.github.com",
    github_base: str = "https://github.com",
    allow_http_loopback: bool = False,
) -> dict[str, Any]:
    """Synchronize the marketplace catalog and all referenced plugin payloads."""
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".sync.lock"
    with lock_path.open("a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        return _sync_mirror_locked(
            output_dir,
            registry_url=registry_url,
            registry_file=registry_file,
            mirror_domain=mirror_domain,
            github_token=github_token,
            max_bytes=max_bytes,
            force=force,
            prune=prune,
            dry_run=dry_run,
            api_base=api_base.rstrip("/"),
            codeload_base=codeload_base.rstrip("/"),
            github_base=github_base.rstrip("/"),
            allow_http_loopback=allow_http_loopback,
        )


def _sync_mirror_locked(
    output_dir: Path,
    *,
    registry_url: str,
    registry_file: Path | None,
    mirror_domain: str,
    github_token: str,
    max_bytes: int,
    force: bool,
    prune: bool,
    dry_run: bool,
    api_base: str,
    codeload_base: str,
    github_base: str,
    allow_http_loopback: bool,
) -> dict[str, Any]:
    opener = _build_opener(allow_http_loopback=allow_http_loopback)
    state_path = output_dir / ".mirror-state.json"
    prev_state: dict[str, Any] = {}
    if state_path.exists():
        try:
            prev_state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            prev_state = {}

    etag = ""
    if registry_file is not None:
        raw_registry = registry_file.read_bytes()
        if len(raw_registry) > REGISTRY_LIMIT:
            raise MirrorError("local registry.json exceeds 512 KiB limit")
    else:
        req_headers = {"Accept": "application/json"}
        if not force and prev_state.get("etag") and (output_dir / "registry.json").exists():
            req_headers["If-None-Match"] = str(prev_state["etag"])
        status, raw_registry, resp_headers = _http_get_bytes(
            opener,
            registry_url,
            REGISTRY_LIMIT,
            headers=req_headers,
            allow_http_loopback=allow_http_loopback,
        )
        if status == 304:
            return {"status": "not_modified", "plugins": prev_state.get("plugin_count", 0)}
        etag = resp_headers.get("etag", "")

    try:
        catalog = json.loads(raw_registry.decode("utf-8"))
    except Exception as exc:
        raise MirrorError("invalid registry.json") from exc

    if catalog.get("version") != 1 or not isinstance(catalog.get("plugins"), list):
        raise MirrorError("unsupported registry.json version or structure")

    plugins = catalog["plugins"]
    if len(plugins) > 256:
        raise MirrorError("registry.json exceeds 256 plugin entries")

    keep_files: set[Path] = set()
    downloaded_sources = 0
    downloaded_assets = 0
    verified_assets = 0

    for entry in plugins:
        if not isinstance(entry, dict):
            raise MirrorError("invalid plugin entry")
        plugin_id = str(entry.get("id") or "")
        repo_url = str(entry.get("repository") or "")
        ref = str(entry.get("ref") or "HEAD")
        subpath = str(entry.get("path") or "")
        if not plugin_id:
            raise MirrorError("plugin entry missing id")
        if not _valid_local_path(subpath):
            raise MirrorError(f"invalid subpath for {plugin_id}: {subpath}")

        slug = _parse_github_slug(repo_url)

        # 1. Resolve ref -> 40-char commit SHA.
        if COMMIT_SHA_RE.match(ref):
            commit = ref
        else:
            api_url = f"{api_base}/repos{slug}/commits/{urllib.parse.quote(ref, safe='')}"
            headers = {"Accept": "application/vnd.github+json"}
            if github_token:
                headers["Authorization"] = f"Bearer {github_token}"
            _, body, _ = _http_get_bytes(
                opener,
                api_url,
                COMMIT_API_LIMIT,
                headers=headers,
                allow_http_loopback=allow_http_loopback,
            )
            commit_doc = json.loads(body.decode("utf-8"))
            commit = str(commit_doc.get("sha") or "")
            if not COMMIT_SHA_RE.match(commit):
                raise MirrorError(f"invalid commit SHA returned for {plugin_id} ({ref})")

        commit_payload = json.dumps({"sha": commit}, separators=(",", ":")).encode("utf-8")
        for ref_key in {ref, commit}:
            commit_dest = output_dir / "api.github.com" / "repos" / slug.lstrip("/") / "commits" / ref_key
            keep_files.add(commit_dest)
            if not dry_run:
                _atomic_write_bytes(commit_dest, commit_payload)

        # 2. Mirror source tarball from codeload.github.com.
        source_dest = output_dir / "codeload.github.com" / slug.lstrip("/") / "tar.gz" / commit
        keep_files.add(source_dest)
        if not source_dest.exists():
            source_url = f"{codeload_base}{slug}/tar.gz/{commit}"
            if not dry_run:
                _http_download_file(
                    opener,
                    source_url,
                    source_dest,
                    ARCHIVE_LIMIT,
                    allow_http_loopback=allow_http_loopback,
                )
            downloaded_sources += 1

        if dry_run and not source_dest.exists():
            continue

        # 3. Parse spec.toml inside source tarball and mirror every [[artifacts]] entry.
        spec_bytes = extract_spec_toml(source_dest, subpath)
        artifacts = parse_spec_artifacts(spec_bytes)
        for art in artifacts:
            release = art.get("release", "")
            asset = art.get("asset", "")
            expected_sha = art.get("sha256", "").lower()
            if not release or not asset or not SHA256_RE.match(expected_sha):
                raise MirrorError(f"invalid artifact declaration in {plugin_id} spec.toml")
            if "/" in release or "/" in asset or release in {".", ".."} or asset in {".", ".."}:
                raise MirrorError(f"unsafe release/asset name in {plugin_id} spec.toml")

            asset_dest = (
                output_dir
                / "github.com"
                / slug.lstrip("/")
                / "releases"
                / "download"
                / release
                / asset
            )
            keep_files.add(asset_dest)
            if asset_dest.exists() and _file_sha256(asset_dest) == expected_sha:
                verified_assets += 1
                continue

            asset_url = (
                f"{github_base}{slug}/releases/download/"
                f"{urllib.parse.quote(release, safe='')}/{urllib.parse.quote(asset, safe='')}"
            )
            if not dry_run:
                _http_download_file(
                    opener,
                    asset_url,
                    asset_dest,
                    ARCHIVE_LIMIT,
                    expected_sha256=expected_sha,
                    allow_http_loopback=allow_http_loopback,
                )
            downloaded_assets += 1
            verified_assets += 1

    pruned = 0
    if prune and not dry_run:
        pruned = _prune_unreferenced(output_dir, keep_files)

    total_bytes = _dir_size(output_dir)
    if total_bytes > max_bytes:
        raise MirrorError(
            f"mirror size ({total_bytes} bytes) exceeds --max-bytes ({max_bytes}); refusing to publish registry.json"
        )

    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    health = {
        "status": "ok",
        "domain": mirror_domain,
        "updated_at": catalog.get("updated_at", ""),
        "synced_at": now_iso,
        "plugin_count": len(plugins),
        "verified_assets": verified_assets,
        "total_bytes": total_bytes,
    }
    if not dry_run:
        # Publish registry.json LAST so clients never see references to missing files.
        _atomic_write_bytes(output_dir / "registry.json", raw_registry)
        _atomic_write_bytes(
            output_dir / "health.json",
            (json.dumps(health, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        _atomic_write_bytes(
            state_path,
            (
                json.dumps(
                    {
                        "etag": etag,
                        "synced_at": now_iso,
                        "plugin_count": len(plugins),
                    },
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
        )

    return {
        "status": "dry_run" if dry_run else "synced",
        "plugins": len(plugins),
        "downloaded_sources": downloaded_sources,
        "downloaded_assets": downloaded_assets,
        "verified_assets": verified_assets,
        "pruned": pruned,
        "total_bytes": total_bytes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync UniClipboard plugin marketplace registry and verified artifacts to a static mirror directory."
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Static web root directory (e.g. /opt/1panel/apps/openresty/openresty/www/sites/plugins.uniclipboard.app/index)",
    )
    parser.add_argument(
        "--registry-url",
        default=os.environ.get("UNIC_PLUGIN_REGISTRY_URL", DEFAULT_REGISTRY_URL),
        help=f"Remote registry.json URL (default: {DEFAULT_REGISTRY_URL})",
    )
    parser.add_argument(
        "--registry-file",
        type=Path,
        default=None,
        help="Optional local registry.json file instead of fetching --registry-url",
    )
    parser.add_argument(
        "--domain",
        default=DEFAULT_MIRROR_DOMAIN,
        help=f"Mirror domain recorded in health.json (default: {DEFAULT_MIRROR_DOMAIN})",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Maximum mirror directory size in bytes (default: {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument("--force", action="store_true", help="Ignore ETag cache and re-verify all entries")
    parser.add_argument("--prune", action="store_true", help="Remove mirror files not referenced by current registry.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate and plan without writing mirror files")
    args = parser.parse_args(argv)

    try:
        summary = sync_mirror(
            args.output_dir,
            registry_url=args.registry_url,
            registry_file=args.registry_file,
            mirror_domain=args.domain,
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            max_bytes=args.max_bytes,
            force=args.force,
            prune=args.prune,
            dry_run=args.dry_run,
        )
    except MirrorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
