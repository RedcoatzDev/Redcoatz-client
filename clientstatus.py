"""Tell the ladder which crash fixes this client's game install has in place.

The server uses it for one thing: the red coat beside a name in other
players' lobby panels. Filled means Redcoatz with every crash fix applied,
hollow means a fix is missing, no coat means no Redcoatz. A lobby needs
everybody on the same fixes, because a patched and an unpatched game desync
at the moment the bug would have fired.

What is sent: one POST to <server>/v1/clientStatus with the Steam ID and a
{fix module: state} map, plus the player's name, client version and Steam key
as headers. The fix states come from each fix module's status(), which reads
the game's files; nothing from those files is sent, only the state word.
Sent once at start-up in a background thread, never fatal, never retried.
"""

import json
import threading
import urllib.request

from version import USER_AGENT
import steamauth

#: The repairs whose state is reported, by module name. Same names the
#: server's requiredFixes uses.
FIXES = ("fixbuffer", "fixmiles", "fix23e")


def fix_states():
    """{module: state} for every repair that can be checked here; "unknown" where the check fails."""
    out = {}
    for name in FIXES:
        try:
            mod = __import__(name)
            out[name] = str(mod.status().get("state", "unknown"))
        except Exception:                    # noqa: BLE001
            out[name] = "unknown"
    return out


def send(server, player, steam_id, log=print):
    """Post the status in the background. Returns the thread, or None."""
    if not server or not player:
        return None

    def run():
        try:
            # The Steam ID goes as text: the server's field is a string.
            body = json.dumps({"steam_id": str(steam_id or ""),
                               "fixes": fix_states()}).encode("utf-8")
            req = urllib.request.Request(
                server.rstrip("/") + "/v1/clientStatus", data=body,
                method="POST", headers={
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                    # UTF-8 through urllib's Latin-1 headers; see uploader.
                    "X-Player": player.encode("utf-8").decode("latin-1"),
                    **steamauth.headers(),
                })
            with urllib.request.urlopen(req, timeout=15):
                pass
            log("client status sent")
        except Exception as e:               # noqa: BLE001
            log(f"client status not sent: {e!r}")

    t = threading.Thread(target=run, name="clientstatus", daemon=True)
    t.start()
    return t
