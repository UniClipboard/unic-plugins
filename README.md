# UniClipboard Plugin Marketplace Registry

Static catalog (`registry.json`) for UniClipboard's online Plugin Marketplace.

Each entry pins a public plugin repository and immutable commit. UniClipboard verifies every installation locally against the repository's `spec.toml` and SHA-256 digests before admitting the plugin.

## Format (`version: 1`)

- `registry.json`: top-level `{ "version": 1, "updated_at": "YYYY-MM-DD", "plugins": [...] }`.
- Categories: `share`, `text`, `organize`, `sync`.
- Desktop fetches `https://raw.githubusercontent.com/UniClipboard/unic-plugins/main/registry.json`, caches validated entries locally, and falls back cleanly when offline.
