# stegra-to-dmdhub-sync

One-way sync from **Stegra.io** Collections/Routes → **DMD Hub** Folders/GPX files.
Stegra is the source of truth.

## Status

**Phase 1 (read-only) — partial.** Stegra pull works. DMD-side enumeration, diff,
and writes are stubbed pending API recon.

## Install

Requires Python 3.11+. Pick one of these (Homebrew Python blocks bare
`pip install` under PEP 668; this is normal).

**A. pipx (recommended for a CLI).** Isolated venv, `stegra-to-dmdhub-sync` on
your PATH globally:

```bash
brew install pipx        # one-time, if not already installed
pipx ensurepath          # one-time
pipx install -e .
```

**B. Plain venv.** No new tools, but you must activate the venv to use the CLI:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# later sessions: source .venv/bin/activate before running the CLI
```

To upgrade after editing code: `pipx install -e . --force` (option A) or
just re-run the CLI — `-e` is editable so changes take effect immediately
(option B).

## Usage

### `auth` — capture credentials

```bash
# Default: paste Stegra token once, DMD cookies read automatically
# from Chrome's cookie store.
stegra-to-dmdhub-sync auth

# Zero-paste for Stegra: also extract the token from a live Chrome tab
# via AppleScript. Requires the one-time Chrome setting noted below.
stegra-to-dmdhub-sync auth --apple-events

# When the automatic DMD cookie read returns only `cookie_consent` (a
# symptom of Chrome's Application-Bound Encryption blocking the decrypt),
# the prompt will guide you to paste a Cookie header from DevTools.
# Force that flow upfront with:
stegra-to-dmdhub-sync auth --paste-cookies
```

### `pull` — read-only snapshot of your Stegra library

```bash
# Incremental: only fetches changes since the cached cursor.
stegra-to-dmdhub-sync pull

# Override the workdir (default ./sync-data):
stegra-to-dmdhub-sync pull --workdir ~/stegra-snapshots
stegra-to-dmdhub-sync pull -w ~/stegra-snapshots

# Force a full re-pull (cursor=0). The local GPX cache is still respected,
# so files only re-download when their `modified_at` actually changed.
stegra-to-dmdhub-sync pull --full
```

Outputs:

- `<workdir>/snapshots/stegra.json` — full merged state of routes and
  collections, with a `cursor` for the next incremental pull.
- `<workdir>/snapshots/stegra.cursor` — last `max_seq` (mirrors the cursor in
  the JSON, useful for shell scripting).
- `<workdir>/gpx/<route-uuid>.gpx` — per-route GPX cache.

### `inspect` / `plan` / `apply` — not yet implemented

```bash
stegra-to-dmdhub-sync inspect    # DMD-side snapshot (stub)
stegra-to-dmdhub-sync plan       # diff + dry-run preview (stub)
stegra-to-dmdhub-sync apply      # writes — disabled in v1
```

## Auth notes

`stegra-to-dmdhub-sync auth` writes `~/.config/stegra-dmd-sync/auth.json` (mode 0600).

**Stegra access token** — short-lived (~60 min), Azure AD B2C bearer.

  - *Default:* paste once. The command prints a DevTools console snippet that
    copies the token to your clipboard.
  - *With `--apple-events`:* the token is read directly from a running
    `stegra.io` tab via AppleScript, no paste required. One-time setup:
    `Chrome menu bar → View → Developer → Allow JavaScript from Apple Events`,
    and accept the macOS Automation permission prompt the first time.

**DMD Hub cookies** — read automatically from Chrome's local cookie store via
`browser-cookie3`. On macOS Chrome ≥127, Application-Bound Encryption blocks
the HttpOnly session cookies from being decrypted; the tool detects this and
walks you through a one-line DevTools capture: Network panel → any request →
Request Headers → "Cookie:" → Copy value → paste. Pass `--paste-cookies` to
go straight to that flow.

Re-run `stegra-to-dmdhub-sync auth` whenever Stegra calls start returning 401.

## Sync state storage

Each synced GPX in DMD Hub carries an invisible HTML-comment footer in its
**Public Description**:

```html
<!-- stegra-sync:v1:{"route_id":"…","collection_id":"…","modified_at":"…","synced_at":"…"} -->
```

This lets sync identify managed entries and skip unchanged ones via the
Stegra `modified_at` timestamp. Records without this marker are treated as
"unmanaged" and left untouched.

## Identity model

A composite key `(stegra_route_id, stegra_collection_id)` identifies each
synced GPX. Routes in multiple Stegra Collections are duplicated into the
corresponding DMD folders.
