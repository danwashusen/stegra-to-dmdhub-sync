"""Auth bootstrap + persistence.

Non-interactive by default:

- **Stegra access token** — extracted from a live `stegra.io` tab via
  AppleScript talking to Chrome. Requires enabling
  `Chrome menu → View → Developer → Allow JavaScript from Apple Events`
  (one-time).
- **DMD Hub cookies** — read directly from Chrome's local cookie store via
  `browser-cookie3` (handles macOS Keychain decryption).

Falls back to paste prompts if either step fails.

Stored at `~/.config/stegra-sync/auth.json` (mode 0600).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("STEGRA_SYNC_CONFIG_DIR") or
                  Path.home() / ".config" / "stegra-sync")
AUTH_PATH = CONFIG_DIR / "auth.json"

DMD_DOMAIN = "hub.dmdnavigation.com"
STEGRA_HOST = "stegra.io"

# AppleScript that finds the first stegra.io tab in Chrome and extracts the
# MSAL access token from localStorage. Returns a sentinel string on failure.
_STEGRA_TOKEN_APPLESCRIPT = r'''
tell application "Google Chrome"
    if not running then return "NOT_RUNNING"
    set jsCode to "(function(){try{var k=Object.keys(localStorage).find(function(k){return k.indexOf('accesstoken')>=0});if(!k)return 'NO_KEY';var o=JSON.parse(localStorage.getItem(k));return o.secret||o.accessToken||o.access_token||'NO_SECRET'}catch(e){return 'ERR:'+e.message}})()"
    repeat with w in windows
        repeat with t in tabs of w
            try
                set theURL to URL of t
            on error
                set theURL to ""
            end try
            if theURL contains "stegra.io" then
                try
                    set theResult to (execute t javascript jsCode)
                    return theResult
                on error errMsg
                    return "JS_DENIED:" & errMsg
                end try
            end if
        end repeat
    end repeat
    return "NO_TAB"
end tell
'''


@dataclass
class AuthBundle:
    stegra_token: str
    stegra_token_expires_at: int = 0   # epoch seconds; 0 = unknown
    dmd_cookies: dict[str, str] = field(default_factory=dict)

    def stegra_token_likely_expired(self, skew_seconds: int = 60) -> bool:
        if not self.stegra_token_expires_at:
            return False
        return time.time() + skew_seconds >= self.stegra_token_expires_at


# ---------- persistence ----------

def load() -> Optional[AuthBundle]:
    if not AUTH_PATH.exists():
        return None
    data = json.loads(AUTH_PATH.read_text())
    return AuthBundle(
        stegra_token=data["stegra_token"],
        stegra_token_expires_at=data.get("stegra_token_expires_at", 0),
        dmd_cookies=data.get("dmd_cookies", {}),
    )


def save(bundle: AuthBundle) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_PATH.write_text(json.dumps({
        "stegra_token": bundle.stegra_token,
        "stegra_token_expires_at": bundle.stegra_token_expires_at,
        "dmd_cookies": bundle.dmd_cookies,
    }, indent=2))
    AUTH_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------- bootstrap ----------

class AuthError(RuntimeError):
    """Raised with a user-actionable message when auto-extraction fails."""


def bootstrap(
    use_apple_events: bool = False,
    allow_paste: bool = True,
    paste_cookies: bool = False,
) -> AuthBundle:
    """Capture a Stegra access token and write auth.json.

    Stegra token comes from one of:
      - AppleScript-to-Chrome (`use_apple_events=True`, opt-in)
      - paste prompt (default)

    The `paste_cookies` parameter is accepted for backwards compatibility
    with older callers but is now a no-op — DMD cookies are no longer
    needed for the local-folder sync flow.
    """
    token: Optional[str] = None
    if use_apple_events:
        print("Reading Stegra access token from Chrome via AppleScript...")
        token, token_err = _read_stegra_token_from_chrome()
        if token is None:
            if not allow_paste:
                raise AuthError(token_err or "Couldn't read Stegra token.")
            print(f"  -> {token_err}")

    if token is None:
        if not allow_paste:
            raise AuthError("No Stegra token (paste disabled).")
        if not use_apple_events:
            print("Stegra access token:")
            print("  1. Open https://stegra.io/map-studio/v2 (sign in if needed).")
            print("  2. Open DevTools (Cmd-Opt-I) → Console, paste:")
            print("     copy(JSON.parse(localStorage.getItem("
                  "Object.keys(localStorage).find(k=>k.includes('accesstoken'))"
                  ")).secret)")
            print("  3. The token is now in your clipboard.")
        token = _paste_prompt("Paste Stegra token: ")
        if not token:
            raise AuthError("No Stegra token entered.")
    exp = _decode_jwt_exp(token)
    remaining = max(0, exp - int(time.time())) if exp else None
    if remaining is not None:
        print(f"  -> token OK, valid for ~{remaining // 60} more minutes")
    else:
        print("  -> token OK (expiry unknown)")

    # DMD cookies aren't needed for the local-sync flow. The helper code
    # (_read_dmd_cookies_from_chrome, _prompt_cookies_via_devtools) is left in
    # place in case we revisit a remote target later.
    _ = paste_cookies  # accepted for backwards compatibility

    bundle = AuthBundle(
        stegra_token=token,
        stegra_token_expires_at=exp,
        dmd_cookies={},
    )
    save(bundle)
    print(f"\nSaved {AUTH_PATH}")
    return bundle


# ---------- Stegra: AppleScript to Chrome ----------

def _read_stegra_token_from_chrome() -> tuple[Optional[str], Optional[str]]:
    """Returns (token, error_message). token is None on failure."""
    if sys.platform != "darwin":
        return None, "AppleScript path is macOS-only."
    if shutil.which("osascript") is None:
        return None, "osascript not found."
    try:
        proc = subprocess.run(
            ["osascript", "-e", _STEGRA_TOKEN_APPLESCRIPT],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None, "Chrome AppleScript timed out."
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        return None, f"Chrome AppleScript failed: {err or output}"
    if output == "NOT_RUNNING":
        return None, "Chrome is not running."
    if output == "NO_TAB":
        return None, ("No stegra.io tab open. Open "
                      "https://stegra.io/map-studio/v2 and sign in first.")
    if output.startswith("JS_DENIED"):
        return None, ("Chrome blocked the script. Enable: View → Developer → "
                      "Allow JavaScript from Apple Events, then retry.")
    if output in ("NO_KEY", "NO_SECRET"):
        return None, ("Couldn't find an MSAL access token in localStorage — "
                      "make sure you're signed into stegra.io.")
    if output.startswith("ERR:"):
        return None, f"JS error in Chrome: {output[4:]}"
    # Sanity-check the token looks like a JWT
    if output.count(".") != 2 or len(output) < 40:
        return None, f"Unexpected token shape (len={len(output)})."
    return output, None


# ---------- DMD: browser-cookie3 ----------

def _read_dmd_cookies_from_chrome() -> tuple[Optional[dict[str, str]], Optional[str]]:
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        return None, "browser-cookie3 not installed."
    try:
        jar = browser_cookie3.chrome(domain_name=DMD_DOMAIN)
    except Exception as e:
        return None, f"browser_cookie3 failed: {e}"
    out: dict[str, str] = {}
    for c in jar:
        if DMD_DOMAIN in (c.domain or ""):
            out[c.name] = c.value
    if not out:
        return None, (f"No cookies for {DMD_DOMAIN} in Chrome cookie store. "
                      "Sign in via Chrome and try again.")
    return out, None


# ---------- DMD: DevTools paste fallback ----------

# Names we treat as "this is a non-session cookie" — purely advisory; if a
# captured cookie set contains ONLY names like these, we assume the session
# cookies weren't extracted and prompt the user to paste.
_BENIGN_COOKIE_NAMES = {"cookie_consent", "cookieconsent", "_ga", "_gid", "_gat"}


def _cookies_look_useful(cookies: dict[str, str]) -> bool:
    """Heuristic: do these look like a working session?

    True if any cookie name suggests a session/auth identifier, OR if the set
    contains more than just well-known benign names.
    """
    if not cookies:
        return False
    session_hints = ("session", "sess", "auth", "token", "login",
                     "phpsessid", "sid", "jwt")
    for name in cookies:
        lower = name.lower()
        if any(hint in lower for hint in session_hints):
            return True
    return any(name not in _BENIGN_COOKIE_NAMES for name in cookies)


def _prompt_cookies_via_devtools(seed: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Walk the user through grabbing the Cookie header from DevTools."""
    print()
    print("─" * 60)
    print(" DMD Hub cookies — DevTools capture")
    print("─" * 60)
    print("1. Open https://hub.dmdnavigation.com/account/profile/gpx/ in Chrome.")
    print("2. Open DevTools (Cmd-Opt-I), Network tab.")
    print("3. Reload the page (Cmd-R).")
    print("4. Click any request to hub.dmdnavigation.com in the list.")
    print("5. Scroll to 'Request Headers' and find the 'Cookie:' line.")
    print("6. Right-click the cookie value → Copy value.")
    print("7. Paste it below (one line, ends in '; ...'):")
    print()
    raw = _paste_prompt("Cookie header: ").strip()
    if not raw:
        return seed or {}
    parsed = _parse_cookies_input(raw)
    # Merge with any non-session cookies we already had — keeps cookie_consent
    # etc. in case the server needs them.
    merged = dict(seed or {})
    merged.update(parsed)
    return merged


def _parse_cookies_input(s: str) -> dict[str, str]:
    """Accept JSON dict, Cookie header (`a=1; b=2`), or newline-separated pairs."""
    s = s.strip()
    if not s:
        return {}
    if s.startswith("{"):
        return json.loads(s)
    out: dict[str, str] = {}
    # Treat both semicolons and newlines as separators
    for chunk in s.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        # Strip a leading "Cookie:" if the user copied the whole header
        if chunk.lower().startswith("cookie:"):
            chunk = chunk.split(":", 1)[1].strip()
            if "=" not in chunk:
                continue
        name, _, value = chunk.partition("=")
        out[name.strip()] = value.strip()
    return out


# ---------- helpers ----------

def _paste_prompt(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _decode_jwt_exp(token: str) -> int:
    """Best-effort parse of `exp` claim from a JWT. Returns 0 if unavailable."""
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0
    exp = payload.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0
