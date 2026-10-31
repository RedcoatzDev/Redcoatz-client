"""Sign in through Steam, once, so the server knows who is reporting.

The player signs in on steamcommunity.com in their own browser, Steam tells
the ladder server which account it was, and the server hands this client a key
tied to that account. Every request then carries the key, and no password ever
touches Redcoatz.

    ladder_overlay.sign_in_screen(root, server, ...)    at start-up
    steamauth.headers()                                on every request
    steamauth.sign_out(server)                         Redcoatz.exe --sign-out

How the key gets here: sign_in listens on 127.0.0.1 on a free port, opens the
default browser at <server>/auth/steam?port=<it>, and once Steam has vouched
for the account the server redirects the browser to
http://127.0.0.1:<port>/redcoatz?key=...&steam_id=... Only a program on this
machine can receive that. The listener accepts nothing else and is torn down
as soon as the key arrives or the wait ends.

On disk: the key, Steam ID, issue time and in-game name are kept in
%APPDATA%\\ntw3ladder\\auth.json, the only file this module reads or writes.
The key is a bearer secret: anyone holding it can report as this account, so
it never goes anywhere but the ladder server, over HTTPS, as an Authorization
header. Nothing from Steam's own files is read here.

Network: POST <server>/v1/signout (revoke the key), GET <server>/v1/whoami
(which account the key proves). sign_out revokes the key on the server and
deletes the file; nothing happens on Steam's side, because Steam only vouched
for the account once and kept no connection to Redcoatz.
"""
import http.server
import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

from version import USER_AGENT

STATE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder")
AUTH_FILE = os.path.join(STATE_DIR, "auth.json")

#: How long the sign-in window waits for the browser, in seconds.
SIGN_IN_TIMEOUT = 240

_cache = None


def load():
    """Read auth.json once and cache it. Returns {"key", "steam_id", "issued", ...} or {}."""
    global _cache
    if _cache is None:
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f) or {}
        except Exception:                        # noqa: BLE001
            _cache = {}
    return _cache


def save(doc):
    """Write auth.json and update the cache."""
    global _cache
    _cache = dict(doc)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    except OSError as e:
        print(f"  (could not save the sign-in: {e})")


def forget():
    """Empty auth.json, discarding the key."""
    save({})


def sign_out(server, timeout=10, log=print):
    """Disconnect: POST <server>/v1/signout to revoke the key, then forget it here.

    Returns True when the server confirmed the revocation. The file goes
    either way, so a key the server never heard of (or a server that is
    down) still leaves this machine signed out.
    """
    doc = load()
    if not doc.get("key"):
        log("steam sign-in: nothing to sign out of")
        return True
    revoked = False
    req = urllib.request.Request(
        server.rstrip("/") + "/v1/signout", data=b"", method="POST",
        headers={"User-Agent": USER_AGENT, **headers()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            revoked = bool(json.loads(r.read().decode("utf-8")).get("revoked"))
    except Exception as e:                       # noqa: BLE001
        log(f"steam sign-in: the server could not revoke the key: {e!r}")
    forget()
    log(f"steam sign-in: signed out of {doc.get('steam_id')}"
        + ("" if revoked else " (the server did not confirm; the key is forgotten here)"))
    return revoked


def headers():
    """The Authorization: Bearer header for a request, or {} when not signed in."""
    key = load().get("key")
    return {"Authorization": "Bearer " + key} if key else {}


def whoami(server, timeout=10):
    """GET <server>/v1/whoami: the Steam ID the server says our key proves, or "" if none or no answer."""
    if not headers():
        return ""
    req = urllib.request.Request(
        server.rstrip("/") + "/v1/whoami",
        headers={"User-Agent": USER_AGENT, **headers()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(json.loads(r.read().decode("utf-8")).get("steam_id") or "")
    except Exception:                            # noqa: BLE001
        return ""


class _Listener(http.server.BaseHTTPRequestHandler):
    """The localhost page the browser is redirected to: takes the key from the query string, says thanks."""
    got = None

    def do_GET(self):                            # noqa: N802
        """Accept /redcoatz?key=...&steam_id=... and answer 404 to anything else."""
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/redcoatz" and q.get("key") and q.get("steam_id"):
            _Listener.got = {"key": q["key"][0], "steam_id": q["steam_id"][0]}
            body = _page("Signed in", (
                "<p>Successfully connected to Redcoatz through Steam.</p>"
                "<p>You can close this tab.</p>"))
            code = 200
        else:
            body = _page("Nothing here", "<p>This address only serves the end "
                         "of a Steam sign-in. You can close this tab.</p>")
            code = 404
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):                   # quiet
        pass


# The same plate as the server's /auth/steam page, so the sign-in starts and
# ends in what looks like the same place. Fonts come from Google as on the
# site; Georgia stands in offline.
_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Redcoatz: {title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@600;700&family=EB+Garamond:ital,wght@0,400;0,500;1,400&family=Marcellus+SC&display=swap" rel="stylesheet">
<style>
:root{--ink:#10131c;--coat:#171c29;--gilt:#c6a15b;--gilt-bright:#e4c67e;--pipeclay:#ece6d7;
--pipeclay-dim:#cbc4b2;--slate:#8a93a6;--rule:rgba(198,161,91,.3);--rule-soft:rgba(198,161,91,.14);color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--ink);color:var(--pipeclay);font:18px/1.62 "EB Garamond",Georgia,serif;
-webkit-font-smoothing:antialiased;padding:32px 16px}
.plate{position:relative;max-width:560px;width:100%;padding:40px 36px 34px;border:1px solid var(--rule);text-align:center}
.plate::before{content:"";position:absolute;inset:9px;border:1px solid var(--rule-soft);pointer-events:none}
.eyebrow{display:block;font-family:"Marcellus SC",Georgia,serif;letter-spacing:.24em;font-size:.68rem;text-transform:uppercase;color:var(--gilt);margin-bottom:10px}
h1{margin:0 0 4px;font-family:"Cormorant Garamond",Georgia,serif;font-weight:700;font-size:2.6rem;letter-spacing:.02em;line-height:1;color:var(--pipeclay)}
h1 .z{color:var(--gilt-bright)}
h2{margin:22px 0 10px;font-family:"Marcellus SC",Georgia,serif;font-weight:400;letter-spacing:.14em;text-transform:uppercase;font-size:1rem;color:var(--gilt-bright)}
p{margin:0 0 12px;text-align:left}
.fine{font-size:.85rem;color:var(--slate);margin:0}
code{font-size:.85em;color:var(--pipeclay-dim);white-space:nowrap}
.rule{height:1px;background:var(--rule-soft);margin:18px 0 0}
.foot{font-family:"Marcellus SC",Georgia,serif;letter-spacing:.14em;font-size:.6rem;text-transform:uppercase;color:var(--slate);margin:14px 0 0;text-align:center}
.foot a{color:var(--gilt);text-decoration:none}
</style></head>
<body><main class="plate">
<span class="eyebrow">An unofficial community project for NTW3</span>
<h1>REDCOAT<span class="z">Z</span></h1>
<h2>{title}</h2>
{body}
<div class="rule"></div>
<p class="foot"><a href="https://redcoatz.com">redcoatz.com</a> &middot; not associated with Valve or Creative Assembly</p>
</main></body></html>
"""


def _page(title, body):
    """Fill the HTML shell with a title and body."""
    return _SHELL.replace("{title}", title).replace("{body}", body)


def _free_port():
    """Ask the OS for a free TCP port on 127.0.0.1."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sign_in(server, log=print, open_browser=webbrowser.open, on_start=None):
    """Run the browser sign-in. Returns the Steam ID signed in as, or None.

    Starts a localhost listener, opens the default browser at
    <server>/auth/steam?port=<port>, and blocks until the key arrives, the
    wait times out, or the caller gives up. `on_start(url, cancel)`, if given,
    is called once the browser has been sent: `url` reopens the page and
    setting the `cancel` event gives up. The listener is torn down either
    way, and the key is saved to auth.json on success.
    """
    _Listener.got = None
    port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), _Listener)
    httpd.timeout = 0.5
    url = server.rstrip("/") + "/auth/steam?port=" + str(port)
    log(f"steam sign-in: listening on 127.0.0.1:{port}, opening {url}")

    done = threading.Event()

    def serve():
        deadline = time.monotonic() + SIGN_IN_TIMEOUT
        while not done.is_set() and _Listener.got is None and time.monotonic() < deadline:
            httpd.handle_request()
        httpd.server_close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        open_browser(url)
    except Exception as e:                       # noqa: BLE001
        log(f"steam sign-in: could not open a browser: {e!r}")
    if on_start is not None:
        on_start(url, done)

    deadline = time.monotonic() + SIGN_IN_TIMEOUT
    while _Listener.got is None and not done.is_set() and time.monotonic() < deadline:
        time.sleep(0.2)
    done.set()
    t.join(timeout=2)
    got = _Listener.got
    if not got:
        log("steam sign-in: nothing came back")
        return None
    save({"key": got["key"], "steam_id": got["steam_id"],
          "issued": time.strftime("%Y-%m-%dT%H:%M:%S")})
    log(f"steam sign-in: signed in as {got['steam_id']}")
    return got["steam_id"]


def remember_name(steam_id, name):
    """Store the in-game name in auth.json, so the panel can say who is signed in.

    Only when the game's Steam ID matches the key's: on a shared PC the name
    read from the game may belong to a different account.
    """
    doc = load()
    if not doc.get("key") or not name or str(steam_id) != str(doc.get("steam_id")):
        return
    if doc.get("name") != name:
        doc["name"] = name
        save(doc)


def key_for(steam_id=None, log=print):
    """The Steam ID the key on file proves, if it is good for this player.

    None when there is no key, or when it belongs to a different account than
    the one the game is signed into; either way the caller has to run sign_in.
    """
    doc = load()
    have = doc.get("steam_id") if doc.get("key") else None
    if have and (not steam_id or str(steam_id) == str(have)):
        log(f"steam sign-in: key on file for {have}")
        return have
    if have:
        log(f"steam sign-in: key on file is for {have}, the game says "
            f"{steam_id}; signing in again")
    return None
