# UniClipboard Plugin Marketplace Mirror (`plugins.uniclipboard.app`)

Static mirror deployment for accelerating UniClipboard plugin marketplace browsing and installation for users in China.

## Why a Full Static Mirror

Installing a plugin from UniClipboard's marketplace accesses four GitHub endpoints:

1. `raw.githubusercontent.com/UniClipboard/unic-plugins/main/registry.json` (catalog)
2. `api.github.com/repos/<owner>/<repo>/commits/<ref>` (ref resolution)
3. `codeload.github.com/<owner>/<repo>/tar.gz/<commit>` (source archive + `spec.toml`)
4. `github.com/<owner>/<repo>/releases/download/<release>/<asset>` (prebuilt binaries)

`sync_mirror.py` mirrors only the plugins listed in `registry.json`, verifies every release asset against the `sha256` declared in `spec.toml`, and publishes them into a static directory layout matching `unic`'s mirror transport:

```text
<web-root>/
├── registry.json                                                 # Published last (atomic rename)
├── health.json                                                   # Sync status metadata
├── api.github.com/repos/<owner>/<repo>/commits/<ref>             # {"sha":"<40-hex>"}
├── codeload.github.com/<owner>/<repo>/tar.gz/<commit>            # Immutable source tarball
└── github.com/<owner>/<repo>/releases/download/<release>/<asset> # Immutable verified binary tarball
```

Because it is purely static and allowlisted to `registry.json`, it cannot be abused as an open GitHub proxy and never hits anonymous GitHub API rate limits on user installs.

## Deploying on Ubuntu + 1Panel OpenResty (`plugins.uniclipboard.app`)

### 1. DNS

Create an `A` record for `plugins.uniclipboard.app` pointing directly to your Singapore/overseas mirror host IP.

> **Important:** Do **not** enable Cloudflare proxy (orange cloud) on `plugins.uniclipboard.app`, otherwise mainland China traffic routes through Cloudflare free-tier nodes instead of directly to the Singapore host.

### 2. Create the 1Panel Static Site

1. In 1Panel, create a static website for `plugins.uniclipboard.app` and enable HTTPS (Let's Encrypt).
2. Confirm its site root is `/opt/1panel/apps/openresty/openresty/www/sites/plugins.uniclipboard.app/index` (or pass `MIRROR_WEB_ROOT=...` to `install.sh`).
3. Add the rules from [`openresty/plugins.uniclipboard.app.conf`](openresty/plugins.uniclipboard.app.conf) into the site's OpenResty `server { ... }` configuration and reload OpenResty.

### 3. Install the Periodic Synchronizer

```bash
git clone https://github.com/UniClipboard/unic-plugins.git
cd unic-plugins
sudo bash mirror/install.sh
```

- Requires only **Python 3.10+ standard library** (no `pip` packages).
- Runs every 10 minutes via `unic-plugins-mirror.timer` using `If-None-Match` (ETag) so idle runs cost almost zero bandwidth.
- Enforces a 2 GiB safety cap (`--max-bytes 2147483648`) and `--prune` to remove stale artifacts.
- Optional: if any catalog entry uses a branch/tag ref instead of a 40-char commit SHA, put `GITHUB_TOKEN=<read-only-public-token>` in `/etc/unic-plugins-mirror.env` (chmod `0600`).

### 4. Verify Deployment

```bash
# Check sync health and catalog
curl -fsSL https://plugins.uniclipboard.app/health.json
curl -fsSL https://plugins.uniclipboard.app/registry.json | head -n 20

# Check systemd timer & logs
systemctl status unic-plugins-mirror.timer
journalctl -u unic-plugins-mirror.service -n 50 --no-pager
```

### 5. Using the Mirror in UniClipboard

- **Desktop**: Settings → Plugins → More (`…`) → Execution Order & Advanced Settings → Plugin Marketplace Mirror → **Official Mirror (`plugins.uniclipboard.app`)**.
- **CLI / `config.toml`**:

```sh
unic config set plugins.mirror_url '"https://plugins.uniclipboard.app"'
```

- **Environment override**:

```sh
UNIC_PLUGIN_MIRROR_URL=https://plugins.uniclipboard.app unic plugin install https://github.com/UniClipboard/unic-plugin-wenshushu --ref <commit>
```

## Local Tests

```bash
python3 -m unittest discover -s mirror -p 'test_*.py' -v
```
