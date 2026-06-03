# stegra-dmd-sync

One-way sync from **Stegra.io** Collections/Routes → **DMD Hub** Folders/GPX files.
Stegra is the source of truth.

## Status

**Phase 1 (read-only) — partial.** Stegra pull works. DMD-side enumeration, diff,
and writes are stubbed pending API recon.

## Install

Requires Python 3.11+. Pick one of these (Homebrew Python blocks bare
`pip install` under PEP 668; this is normal).

**A. pipx (recommended for a CLI).** Isolated venv, `sync` on your PATH globally:

```bash
brew install pipx        # one-time, if not already installed
pipx ensurepath          # one-time
pipx install -e .
```

**B. Plain venv.** No new tools, but you must activate the venv to use `sync`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# later sessions: source .venv/bin/activate before running `sync`
```

To upgrade after editing code: `pipx reinstall stegra-dmd-sync` (option A) or
just re-run `sync` — `-e` is editable so changes take effect immediately
(option B).

## Usage

```bash
# Once per ~60 minutes (Stegra access tokens expire):
stegra-to-dmdhub-sync auth

# Read-only snapshot of your Stegra library + GPX files:
stegra-to-dmdhub-sync pull

# Not yet implemented:
stegra-to-dmdhub-sync inspect    # DMD-side snapshot
stegra-to-dmdhub-sync plan       # diff + dry-run preview
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
`browser-cookie3` (handles macOS Keychain decryption). Falls back to a paste
prompt if the read fails (e.g. Chrome is encrypting cookies under a new
scheme).

Re-run `stegra-to-dmdhub-sync auth` whenever Stegra calls start returning 401.

Credentials are stored at `~/.config/stegra-dmd-sync/auth.json` (mode 0600).

## Sync state storage

Each synced GPX in DMD Hub carries an invisible HTML-comment footer in its
**Public Description**:

```html
<!-- stegra-to-dmdhub-sync:v1:{"route_id":"…","collection_id":"…","modified_at":"…","synced_at":"…"} -->
```

This lets sync identify managed entries and skip unchanged ones via the
Stegra `modified_at` timestamp. Records without this marker are treated as
"unmanaged" and left untouched.

## Identity model

A composite key `(stegra_route_id, stegra_collection_id)` identifies each
synced GPX. Routes in multiple Stegra Collections are duplicated into the
corresponding DMD folders.
