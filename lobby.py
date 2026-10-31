"""Work out who is currently in the NTW3 lobby, from ChatValentin's state files.

    python lobby.py            print what is readable right now
    python lobby.py --watch    print whenever it changes

The game does not publish a lobby roster, but the ChatValentin mod keeps its
state in plain text files beside its executables, under
<Steam library>\\steamapps\\common\\Napoleon Total War\\data\\NTW3\\ChatValentin:

    plinfo.txt      player info
    lbinfo.txt      lobby info
    lvlb.txt        ?
    equipe.txt      "team" in French
    pl2lbinfo.txt   one "name steamid-in-hex" record per line

This module only reads those files, and only to extract display names and
SteamID64s. It never writes to them and sends nothing anywhere. The files are
empty while the game is closed, so the parsers are deliberately tolerant of
format: `--watch --raw` shows the live contents.
"""

import argparse
import json
import os
import re
import time

def _chatvalentin_dir():
    """Locate the ChatValentin folder in whichever Steam library holds the game.

    Library roots come from gamelaunch._steam_roots; the default C: install
    is named if nothing is found, so the diagnostics have a path to report
    as missing.
    """
    tail = os.path.join("steamapps", "common", "Napoleon Total War",
                        "data", "NTW3", "ChatValentin")
    try:
        import gamelaunch
        for root in gamelaunch._steam_roots():
            found = os.path.join(root, tail)
            if os.path.isdir(found):
                return found
    except Exception:                            # noqa: BLE001
        pass
    return os.path.join(r"C:\Program Files (x86)\Steam", tail)


DEFAULT_DIR = _chatvalentin_dir()

STATE_FILES = ("plinfo.txt", "lbinfo.txt", "pl2lbinfo.txt", "equipe.txt", "lvlb.txt")

# A player name as NTW3 allows them: letters, digits, and the punctuation that
# turns up in clan tags such as "[BNR] G3R4LD D4RM4NIN" or "Noel Gallagher(N)".
NAME_RE = re.compile(r"[A-Za-z0-9_\[\]\(\)\.\-' ]{3,32}")

# ChatValentin writes SteamID64s as hex. Every individual SteamID64 is
# 0x0110000100000000 plus the account id, so they all begin "1100001" in hex;
# anchoring on that avoids mistaking other hex-ish tokens for identities.
STEAMID_RE = re.compile(r"\b(1100001[0-9a-fA-F]{8,9})\b")
STEAMID64_BASE = 76561197960265728


def steam_ids(state):
    """Every SteamID64 mentioned across the state files, in order, deduped."""
    ids, seen = [], set()
    for name in ("lbinfo.txt", "pl2lbinfo.txt", "plinfo.txt", "lvlb.txt", "equipe.txt"):
        for m in STEAMID_RE.findall(state.get(name, "")):
            v = int(m, 16)
            if v > STEAMID64_BASE and v not in seen:
                seen.add(v)
                ids.append(v)
    return ids


def read_state(directory=DEFAULT_DIR):
    """Read each state file in the ChatValentin folder; returns {name: contents}, skipping missing ones."""
    out = {}
    for name in STATE_FILES:
        p = os.path.join(directory, name)
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                out[name] = f.read()
        except OSError:
            continue
    return out


def parse_roster(state):
    """Players as (name, steamid64) pairs.

    pl2lbinfo.txt holds one record per line: the display name, a space, and
    the SteamID64 in hex. Names contain spaces themselves ("[BNR] My
    Clanker"), so the id is found by pattern rather than by splitting.
    """
    out, seen = [], set()
    for fname in ("pl2lbinfo.txt", "lbinfo.txt", "plinfo.txt"):
        for line in state.get(fname, "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = STEAMID_RE.search(line)
            if not m:
                continue
            sid = int(m.group(1), 16)
            name = line[:m.start()].strip() or line[m.end():].strip()
            # A bare id with no name is still a seat in the lobby; show the id
            # rather than dropping the player.
            if not name or name.lower() in ("true", "false"):
                name = str(sid)
            if sid in seen:
                continue
            seen.add(sid)
            out.append((name, sid))
    return out


def parse_players(state):
    """Best-effort list of names from whatever the state files contain.

    Tries JSON, then one record per line, then scrapes name-shaped tokens.
    Order is preserved because lobby order usually reflects the teams.
    """
    text = ""
    for key in ("plinfo.txt", "pl2lbinfo.txt", "lbinfo.txt"):
        if state.get(key, "").strip():
            text = state[key]
            break
    if not text.strip():
        return []

    # JSON, if we are lucky.
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x) for x in data][:16]
        if isinstance(data, dict):
            for k in ("players", "joueurs", "list"):
                if isinstance(data.get(k), list):
                    return [str(x) for x in data[k]][:16]
    except ValueError:
        pass

    # One record per line: take the first field of each.
    names, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        first = re.split(r"[;,|\t]", line)[0].strip()
        if 3 <= len(first) <= 32 and first.lower() not in seen:
            seen.add(first.lower())
            names.append(first)
    if names:
        return names[:16]

    # Last resort: scrape anything name-shaped.
    for m in NAME_RE.findall(text):
        m = m.strip()
        if 3 <= len(m) <= 32 and m.lower() not in seen:
            seen.add(m.lower())
            names.append(m)
    return names[:16]


def main():
    """Command-line entry point: print the state files once, or whenever they change."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR, help="ChatValentin directory")
    ap.add_argument("--watch", action="store_true", help="report on change")
    ap.add_argument("--raw", action="store_true", help="dump raw file contents")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"not found: {args.dir}")
        print("ChatValentin does not appear to be installed; pass --dir")
        return

    def report(state):
        for name, body in state.items():
            n = len(body)
            print(f"  {name:<16} {n:>6} bytes" + ("" if n else "   (empty)"))
            if args.raw and n:
                for line in body.splitlines()[:12]:
                    print(f"      | {line}")
        players = parse_players(state)
        print(f"  -> roster: {players if players else '(none readable)'}")

    if not args.watch:
        print(f"reading {args.dir}")
        report(read_state(args.dir))
        print("\nIf every file is empty, NTW3 is not running or not in a lobby.")
        print("Run with --watch --raw, then join a lobby, to capture the format.")
        return

    print("watching; join an NTW3 lobby. Ctrl-C to stop.")
    last = None
    while True:
        state = read_state(args.dir)
        key = json.dumps({k: v for k, v in state.items()}, sort_keys=True)
        if key != last:
            last = key
            print(f"\n[{time.strftime('%H:%M:%S')}] changed")
            report(state)
        time.sleep(2)


if __name__ == "__main__":
    main()
