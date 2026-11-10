"""EVE SSO login for endpoints that need a character (player-owned citadel markets).

    python -m eve_market login      # opens EVE's login page in your browser, saves a refresh token
    python -m eve_market whoami     # shows which character is logged in

Credentials come from a local `.env` file (see `.env.example`); the token is saved to
data/esi_token.json. Both are git-ignored. Your EVE password only ever goes to EVE's own site.
"""

#This .py is only necessary if you need to gather data that requires a logged in user. But it is not necessary for the market modeling contained in forecast.py
from __future__ import annotations

import base64
import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from eve_market.esi import USER_AGENT

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TOKEN_FILE = ROOT / "data" / "esi_token.json"
AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
SCOPES = ["esi-markets.structure_markets.v1", "esi-universe.read_structures.v1"]


class NotLoggedIn(RuntimeError):
    pass


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in ("EVE_CLIENT_ID", "EVE_CLIENT_SECRET") if not env.get(k)]
    if missing:
        raise NotLoggedIn(f"Set {', '.join(missing)} in {path} (see .env.example).")
    env.setdefault("EVE_CALLBACK_URL", "http://localhost:8765/callback")
    return env


def _basic(env: dict) -> dict:
    raw = f"{env['EVE_CLIENT_ID']}:{env['EVE_CLIENT_SECRET']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(), "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded", "Host": "login.eveonline.com"}


def _claims(access_token: str) -> dict:
    """Decode the JWT payload (no signature check; it came straight from EVE over HTTPS)."""
    payload = access_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _save(tok: dict) -> dict:
    claims = _claims(tok["access_token"])
    data = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + int(tok.get("expires_in", 1199)) - 60,
        "character_id": int(claims["sub"].split(":")[-1]),
        "character_name": claims.get("name"),
        "scopes": claims.get("scp", []),
    }
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def login(timeout: int = 300) -> dict:
    env = load_env()
    callback = urlparse(env["EVE_CALLBACK_URL"])
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "redirect_uri": env["EVE_CALLBACK_URL"], "client_id": env["EVE_CLIENT_ID"],
        "scope": " ".join(SCOPES), "state": state,
    })
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            ok = urlparse(self.path).path == callback.path and q.get("state", [""])[0] == state and "code" in q
            if ok:
                result["code"] = q["code"][0]
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Logged in. You can close this tab." if ok else "Login failed or was cancelled."
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer((callback.hostname, callback.port or 80), Handler)
    server.timeout = 5
    print("Opening EVE's login page in your browser. If it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    deadline = time.time() + timeout
    while "code" not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise NotLoggedIn("Timed out waiting for the EVE login callback.")

    resp = requests.post(TOKEN_URL, headers=_basic(env), timeout=30,
                         data={"grant_type": "authorization_code", "code": result["code"]})
    resp.raise_for_status()
    return _save(resp.json())


def load_token() -> dict:
    if not TOKEN_FILE.exists():
        raise NotLoggedIn("Not logged in. Run `python -m eve_market login` first.")
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def access_token() -> str:
    """A valid access token, refreshing (and saving the rotated refresh token) when needed."""
    tok = load_token()
    if time.time() < tok["expires_at"]:
        return tok["access_token"]
    env = load_env()
    resp = requests.post(TOKEN_URL, headers=_basic(env), timeout=30,
                         data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    if resp.status_code in (400, 401):
        raise NotLoggedIn("Saved login expired or was revoked. Run `python -m eve_market login` again.")
    resp.raise_for_status()
    return _save(resp.json())["access_token"]


def has_login() -> bool:
    try:
        load_env()
        load_token()
        return True
    except NotLoggedIn:
        return False
