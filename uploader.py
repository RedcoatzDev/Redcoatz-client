"""Watch the NTW3 replay folder and upload finished battles to the ladder.

    python uploader.py                     watch forever
    python uploader.py --once              upload anything new, then exit
    python uploader.py --backfill          include replays that predate the client
    python uploader.py --dry-run           show what would be sent

What it touches on disk:

  * %APPDATA%\\The Creative Assembly\\Napoleon\\replays is listed and each
    .replay file in it is opened for reading only. Replays are never moved,
    renamed, modified or deleted.
  * %APPDATA%\\ntw3ladder\\uploaded.json and queue.json are read and written:
    which file hashes have been sent, and which uploads are waiting to retry.
  * Steam's config\\loginusers.vdf is read for the signed-in account's name and
    SteamID64 (see steam_account). Nothing else is read from Steam.
  * ChatValentin's state files beside the game are read for the display name
    (see lobby.py).

What is sent, and only to the ladder server (redcoatz.com unless --server or
NTW3LADDER_SERVER says otherwise): the bytes of each .replay file, POSTed to
/v1/replays with the player's name, the client version and the Steam sign-in
key as headers. Nothing else from the disk leaves the machine. The game's
desync log is sent by desync.py, which this loop calls.

Rules, in order of how much damage breaking them would do: never touch a
player's replay; never lose an upload (failures go to a queue on disk and are
retried across restarts); never upload a half-written file (a replay is ready
only once its size has stopped changing).

Standard library only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lobby import DEFAULT_DIR as CV_DIR, read_state, parse_roster  # noqa: E402
import nettrust  # noqa: E402,F401  imported for its import-time side effect
import consent  # noqa: E402
import steamauth  # noqa: E402
import firstsync  # noqa: E402

# The public ladder by default, so an installed client needs no flag to reach
# it. NTW3LADDER_SERVER overrides it for local development.
DEFAULT_SERVER = os.environ.get("NTW3LADDER_SERVER", "https://redcoatz.com")

REPLAY_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "The Creative Assembly", "Napoleon", "replays",
)
STATE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder"
)
SENT_FILE = os.path.join(STATE_DIR, "uploaded.json")
QUEUE_FILE = os.path.join(STATE_DIR, "queue.json")

SETTLE_SECONDS = 3.0     # size must hold steady this long before we read it
POLL_SECONDS = 5.0
RETRY_BACKOFF = (10, 30, 120, 600, 1800)


def log(msg):
    """Print a timestamped line."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---- identity ---------------------------------------------------------------

def steam_account():
    """(persona name, steamid64) for the Steam account most recently signed in on this machine.

    Reads config\\loginusers.vdf from each Steam library root (see
    _loginusers_paths) and takes the entry flagged MostRecent, or the newest
    timestamp. Only the persona name, SteamID64, timestamp and MostRecent
    flag are looked at; nothing else from Steam is read.

    This identifies the machine's account, which is the point (see
    detect_player). It is not proof of identity: the file is plain text and
    anyone can edit it. Proof comes from the Steam sign-in in steamauth.py.
    """
    best = None                       # (recent, timestamp, name, sid)
    for vdf in _loginusers_paths():
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue                             # not this library; try the next
        for m in re.finditer(r'"(7656\d+)"\s*\{(.*?)\n\t}', text, re.S):
            sid, body = m.group(1), m.group(2)
            name = re.search(r'"PersonaName"\s*"([^"]*)"', body)
            recent = re.search(r'"MostRecent"\s*"(\d)"', body)
            stamp = re.search(r'"Timestamp"\s*"(\d+)"', body)
            cand = (recent is not None and recent.group(1) == "1",
                    int(stamp.group(1)) if stamp else 0,
                    name.group(1) if name else "", int(sid))
            if best is None or cand[:2] > best[:2]:
                best = cand
    if best is None:
        return None, None
    return (best[2] or None), best[3]


def detect_player(explicit=None):
    """Work out who this is, without asking. Returns (name, steamid64).

    Steam decides who; ChatValentin's lobby roster decides what they are
    called. The id comes from the account signed in on this machine, and the
    roster is consulted only for the display name against that id, which is
    the exact name other players see and the ladder is keyed by. Taking a name
    from the roster by position would adopt a lobby-mate's identity whenever
    they were listed first, and file every battle under the wrong person.

    Returns (None, None) when Steam cannot be read rather than guessing from
    the roster: a client with no name is a state the server handles, a client
    with somebody else's name corrupts the archive silently.
    """
    if explicit:
        return explicit, None

    persona, sid = steam_account()
    if not sid:
        return None, None

    # The name others see, for this id and no other.
    try:
        for name, rid in parse_roster(read_state(CV_DIR)):
            if rid == sid and name and not name.isdigit():
                return name, sid
    except Exception:                            # noqa: BLE001 - never fatal
        pass
    return persona, sid


def _loginusers_paths():
    r"""Every config\loginusers.vdf Steam may have written, in the order to try them.

    Steam library roots come from gamelaunch._steam_roots (the registry and
    libraryfolders.vdf), with the default install locations appended in case
    the registry is unreadable. Steam installed on another drive used to make
    detect_player return (None, None), and a client with no name uploads
    nothing for the whole session.
    """
    roots = []
    try:
        import gamelaunch
        roots += list(gamelaunch._steam_roots())
    except Exception:                            # noqa: BLE001
        pass
    roots += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]

    seen, out = set(), []
    for r in roots:
        p = os.path.join(r, "config", "loginusers.vdf")
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


# ---- persistent state -------------------------------------------------------

def load_json(path, default):
    """Read a JSON file, returning `default` if it is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    """Write a JSON file atomically, via a .tmp file and os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


# ---- the work ---------------------------------------------------------------

def sha256_file(path):
    """SHA-256 hex digest of a file, read in 1 MiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_settled(path):
    """True once the file has stopped growing.

    The game writes the replay as the battle ends. A truncated upload cannot
    be scored, and because uploads are keyed on content hash the finished
    file would then look like a different battle.
    """
    try:
        a = os.path.getsize(path)
        time.sleep(SETTLE_SECONDS)
        return a == os.path.getsize(path) and a > 0
    except OSError:
        return False


def post_replay(server, path, player, timeout=120):
    """POST the replay's bytes to <server>/v1/replays and return the server's JSON answer.

    Headers carry the player's name (X-Player), the client identifier and
    the Steam sign-in key (steamauth.headers). Nothing but the file's bytes
    goes in the body.
    """
    with open(path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(
        server.rstrip("/") + "/v1/replays",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            # urllib writes header values as Latin-1. Encoding to UTF-8 and
            # decoding as Latin-1 puts the UTF-8 bytes on the wire, so names
            # with non-ASCII characters arrive intact.
            "X-Player": player.encode("utf-8").decode("latin-1"),
            "X-Client": "ntw3ladder-uploader/0.1",
            **steamauth.headers(),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _retry_after(err, default):
    """Seconds the server asked us to wait, from a 429's Retry-After header, else `default`."""
    try:
        return max(60, int(err.headers.get("Retry-After", "")))
    except (TypeError, ValueError, AttributeError):
        return default


def describe(result):
    """One line summarising the server's answer to an upload."""
    m = result.get("match") or {}
    teams = m.get("teams") or []
    alive = " vs ".join(str(t.get("alive_at_end", "?")) for t in teams)
    dup = " (already had it)" if result.get("duplicate") else ""
    return f"{m.get('outcome', '?')} [{alive} alive]{dup}"


class Uploader:
    """Scans a replay folder, uploads new files and keeps the sent list and retry queue on disk."""

    def __init__(self, server, player, replay_dir, dry_run=False):
        self.server = server.rstrip("/")
        self.player = player
        self.replay_dir = replay_dir
        self.dry_run = dry_run
        self.sent = load_json(SENT_FILE, {})       # sha -> {file, at, outcome}
        self.queue = load_json(QUEUE_FILE, [])     # [{path, attempts, next_try}]
        # Until when the server has paused this account (a 429); nothing is
        # posted before then. See upload.
        self.paused_until = 0.0

    def paused(self):
        """True while the server's pause on this account is in force."""
        return time.time() < self.paused_until

    def scan(self, backfill=False, baseline=None):
        """List .replay files in the folder, skipping those older than `baseline` unless backfilling."""
        out = []
        try:
            names = sorted(os.listdir(self.replay_dir))
        except OSError as e:
            log(f"cannot read {self.replay_dir}: {e}")
            return out
        for n in names:
            if not n.lower().endswith(".replay"):
                continue
            p = os.path.join(self.replay_dir, n)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if not backfill and baseline is not None and mtime < baseline:
                continue
            out.append(p)
        return out

    def upload(self, path):
        """Upload one file unless its hash was already sent. True when it needs no retry."""
        try:
            sha = sha256_file(path)
        except OSError as e:
            log(f"cannot read {os.path.basename(path)}: {e}")
            return False
        if sha in self.sent:
            return True
        if self.paused():
            return False                       # queued; see paused_until
        if self.dry_run:
            log(f"would upload {os.path.basename(path)}  ({sha[:8]})")
            return True
        try:
            res = post_replay(self.server, path, self.player)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            # 401 is about this client, not this file: no Steam key. The
            # replay goes up once the player has signed in.
            if e.code == 401:
                log(f"not signed in through Steam; {os.path.basename(path)} waits")
                return False
            # 429 is about this account, not this file: the server has
            # paused it (more than a hundred new replays in a day, which a
            # first sync of a long history can do). Nothing is rejected;
            # the whole queue waits for as long as the server says.
            if e.code == 429:
                wait = _retry_after(e, default=3600)
                self.paused_until = time.time() + wait
                log(f"the server paused this account for {wait // 60} min; "
                    f"{os.path.basename(path)} and the rest wait")
                return False
            # Any other 4xx means the server will never accept this file, so
            # record it as handled rather than retrying forever.
            if 400 <= e.code < 500:
                log(f"REJECTED {os.path.basename(path)}: {e.code} {detail}")
                self.sent[sha] = {"file": os.path.basename(path),
                                  "at": time.time(), "rejected": detail}
                save_json(SENT_FILE, self.sent)
                return True
            log(f"server error {e.code} for {os.path.basename(path)}; will retry")
            return False
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"offline ({e}); queued {os.path.basename(path)}")
            # A certificate failure will not clear by waiting, so say what
            # would fix it (usually antivirus HTTPS scanning; see nettrust).
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                log(f"    {nettrust.tls_hint()}")
            return False

        self.sent[sha] = {"file": os.path.basename(path), "at": time.time(),
                          "outcome": (res.get("match") or {}).get("outcome")}
        save_json(SENT_FILE, self.sent)
        log(f"uploaded {os.path.basename(path)} -> {describe(res)}")
        if res.get("unrated_reason"):
            # Kept by the server but rated for nobody: usually a replay from
            # before a Steam rename, which the server cannot tie to this
            # account. Said once per file so the player knows why.
            log(f"    not rated: {res['unrated_reason']}")
        return True

    def enqueue(self, path):
        """Add a failed upload to the on-disk retry queue, once."""
        for item in self.queue:
            if item["path"] == path:
                return
        self.queue.append({"path": path, "attempts": 0, "next_try": 0})
        save_json(QUEUE_FILE, self.queue)

    def drain_queue(self):
        """Retry queued uploads that are due, with increasing back-off."""
        now = time.time()
        changed = False
        for item in list(self.queue):
            if item["next_try"] > now:
                continue
            if not os.path.exists(item["path"]):
                self.queue.remove(item)
                changed = True
                continue
            if self.upload(item["path"]):
                self.queue.remove(item)
            else:
                item["attempts"] += 1
                delay = RETRY_BACKOFF[min(item["attempts"], len(RETRY_BACKOFF) - 1)]
                item["next_try"] = now + delay
            changed = True
        if changed:
            save_json(QUEUE_FILE, self.queue)


def watch_forever(server, player, replay_dir=None, stop=None):
    """The upload loop, for callers that already have a UI.

    Sweeps once if the first sweep has not happened, then watches. Also runs
    desync.check each poll, since a collapsed battle leaves no replay and the
    desync log is the only record of it. Swallows its own errors: it runs on
    a background thread with nothing on screen to report them.
    """
    up = Uploader(server, player, replay_dir or REPLAY_DIR)
    import desync
    sweep = firstsync.pending()
    baseline = None if sweep else time.time()
    seen = set()
    while stop is None or not stop.is_set():
        if not steamauth.load().get("key"):
            # Signed out: nothing leaves this machine. The server refuses
            # keyless uploads, and a refused upload would otherwise go to the
            # retry queue and hammer the server all session.
            if stop is not None:
                stop.wait(POLL_SECONDS)
            else:
                time.sleep(POLL_SECONDS)
            continue
        try:
            try:
                desync.check(server, player, log=log)
            except Exception as e:
                log(f"desync check failed: {type(e).__name__}: {e}")

            up.drain_queue()
            for path in up.scan(backfill=sweep, baseline=baseline):
                if stop is not None and stop.is_set():
                    return
                if path in seen or not is_settled(path):
                    continue
                seen.add(path)
                if not up.upload(path):
                    up.enqueue(path)
            # A paused account finishes its sweep later: the files are in
            # the queue, and marking the sweep done would mean the ones
            # not yet listed are never looked at again.
            if sweep and not up.paused():
                firstsync.mark_done()
                sweep = False
                baseline = time.time()
        except Exception as e:
            log(f"upload loop: {e}")
        if stop is not None:
            stop.wait(POLL_SECONDS)
        else:
            time.sleep(POLL_SECONDS)


def main():
    """Console entry point: detect the player, require consent, then sweep and watch."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--player", default=None, help="override the detected name")
    ap.add_argument("--replay-dir", default=REPLAY_DIR)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="also upload replays recorded before this client existed")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    player, sid = detect_player(args.player)
    if not player:
        log("could not work out who you are; pass --player NAME")
        return 2
    log(f"player: {player}" + (f"  (steam {sid})" if sid else ""))

    # Before anything is read or sent: the terms describe uploading replays.
    if not consent.require_consent(args.server, player=player, steam_id=sid):
        return 1

    log(f"replays: {args.replay_dir}")
    log(f"server: {args.server}")

    up = Uploader(args.server, player, args.replay_dir, dry_run=args.dry_run)
    log(f"{len(up.sent)} already uploaded, {len(up.queue)} queued")

    # The first run sends what is already on disk so a player's history
    # counts; every run after that sends only new battles. The terms shown
    # before this point say that .replay files are uploaded.
    sweep = args.backfill or firstsync.pending()
    baseline = None if sweep else time.time()
    if sweep:
        log("searching for .replay files (first run: sending what is already "
            "here)")

    seen = set()
    while True:
        up.drain_queue()
        for path in up.scan(backfill=sweep, baseline=baseline):
            if path in seen:
                continue
            if not is_settled(path):
                continue          # still being written; catch it next pass
            seen.add(path)
            if not up.upload(path):
                up.enqueue(path)

        if sweep:
            # One pass is the sweep. Mark it done and drop back to watching.
            log(f"first upload finished ({len(seen)} seen); "
                "now watching for new battles")
            if not args.dry_run:
                firstsync.mark_done()
            sweep = False
            baseline = time.time()

        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print("\nstopped")
