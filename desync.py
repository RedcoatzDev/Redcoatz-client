"""Send the game's desync log to the ladder when a battle collapses.

The game writes %APPDATA%\\The Creative Assembly\\Napoleon\\logs\\mp_battle_desync.txt
only when a multiplayer battle desynchronises, and overwrites it on the next
one. When the host drops nobody gets a replay, so this file is the only record
the battle happened, and it names who stopped reporting.

What is read: that one file, capped at MAX_BYTES. What is written:
%APPDATA%\\ntw3ladder\\desync.json, the hash of the last log dealt with. What
is sent: the log's raw bytes, POSTed to <server>/v1/desyncs with the player's
name, client version and Steam key as headers. The server parses it; sending
the bytes keeps the claim in the game's words, since it names other players.

The first log found on a machine is recorded and skipped, not sent: it may
predate the player's agreement and may be a battle they were not in.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from version import USER_AGENT
import steamauth

#: Where the game keeps it. One file, overwritten each time.
DESYNC_FILE = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "The Creative Assembly", "Napoleon", "logs", "mp_battle_desync.txt")

#: What has already been sent, so the same collapse is not reported twice.
STATE_FILE = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "ntw3ladder", "desync.json")

#: The file is a few hundred bytes. Something much larger is not the file we
#: think it is.
MAX_BYTES = 64 * 1024


def _load():
    """Read desync.json, or {} if missing or unreadable."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(doc):
    """Write desync.json. Losing it costs a duplicate report, which the server drops."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    except OSError:
        pass


def read_current(path=None):
    """The log's bytes and their SHA-256, or (None, None) if there is no log or it is oversized."""
    path = path or DESYNC_FILE
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None, None
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES)
    except OSError:
        return None, None
    if not raw.strip():
        return None, None
    return raw, hashlib.sha256(raw).hexdigest()


def post(server, player, raw, timeout=15):
    """POST the log bytes to <server>/v1/desyncs. Returns the server's JSON answer, or None if it did not land."""
    req = urllib.request.Request(
        f"{server.rstrip('/')}/v1/desyncs",
        data=raw,
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": USER_AGENT,
            # Latin-1 for the same reason uploader.py does it: urllib writes
            # headers as Latin-1, and a name with an accent must not raise.
            "X-Player": (player or "unknown").encode("utf-8").decode("latin-1"),
            **steamauth.headers(),
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def check(server, player, path=None, log=print):
    """Send the log if it is new. Returns True if something was sent.

    Cheap enough to call on every poll: with no new desync it is one stat()
    and a hash of a few hundred bytes.
    """
    raw, digest = read_current(path)
    if not raw:
        return False

    state = _load()
    if state.get("last") == digest:
        return False                      # already dealt with

    # First sighting on this machine: record it, do not send it.
    if not state:
        _save({"last": digest, "at": time.time(), "sent": False})
        log("desync log found from before this client was installed; "
            "recorded, not sent")
        return False

    answer = post(server, player, raw)
    if answer is None:
        # Left unrecorded on purpose so the next poll tries again. The file
        # only changes when another battle collapses, so retrying is cheap.
        log("desync log could not be sent; will retry")
        return False

    _save({"last": digest, "at": time.time(), "sent": True})
    if answer.get("stored"):
        log(f"desync reported: tick {answer.get('tick')}, "
            f"{answer.get('players')} players")
    else:
        # Every player in the battle sends their own copy and the server
        # keeps all of them, so "not stored" means this machine has sent this
        # event before, which a reinstall can do.
        log("desync already reported by this client")
    return True
