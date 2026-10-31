"""Read the lobby roster from the running game.

    python live_seats.py            one read
    python live_seats.py --watch    re-read every few seconds, with timings

The decoding lives in roster.py and is shared with the tests. This module is
the live wrapper: it finds the game, opens a read-only handle, locates the
seat array and hands the bytes to roster.py.

What it touches. The process is Napoleon.exe, found by name through
lobby_memory.find_game. Every reader here opens it with
PROCESS_QUERY_INFORMATION | PROCESS_VM_READ only and closes the handle before
returning. Memory is read with ReadProcessMemory and never written. What is
read is the lobby's seat array: each player's display name, SteamID64,
alliance and position, army preset, and the lobby's configured size. Nothing
is written to disk. The values are returned to the caller; the overlay
(ladder_overlay.py) shows them in its panel and includes them in the game
start and end reports it sends to the ladder server.

The seat array is located again on every poll. Its address changes when the
roster changes, so a cached address keeps decoding whoever was seated when it
was found. Re-locating is cheap because the search is confined to the regions
that held the array's owner last time (_ARRAY_HINTS).
"""

import argparse
import sys
import time

from lobby_memory import (find_game, k32, find_roster_array, roster_owner,
                          PROCESS_QUERY_INFORMATION, PROCESS_VM_READ)
from roster import (LiveSource, lobby_armies, lobby_capacity, lobby_roster,
                    lobby_seats, lobby_seats_detail)


def read_roster(local=None, pid=None):
    """[(name, steamid64)] for the lobby now. [] if not in a lobby.

    Opens the game process read-only, locates the seat array and decodes it.
    `local` is accepted and ignored; it stays in the signature because the
    overlay passes it.
    """
    if sys.platform != "win32":
        return []
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return []
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return []
    try:
        # Through the owner where possible, so the game's own seat count
        # applies and a record left behind by someone who left is not read
        # as a player. See roster.COUNT_OFF.
        arr, bases = find_roster_array(pid, hints=_ARRAY_HINTS or None)
        if not arr and _ARRAY_HINTS:
            arr, bases = find_roster_array(pid, hints=set())
        if bases:
            _ARRAY_HINTS.clear()
            _ARRAY_HINTS.update(bases)
        return lobby_roster(LiveSource(h), local, arr or None)
    finally:
        k32.CloseHandle(h)


#: Regions that held the seat array's owner last time. Memory only.
_ARRAY_HINTS = set()


def read_seats(pid=None, detail=False):
    """[(name, steamid64, alliance, position)] for the lobby now.

    The richer form of read_roster: same players, plus which alliance each is
    in and where they sit in it. With `detail`, also the raw byte those two
    were decoded from and the seat's index in the array (see
    roster.lobby_seats_detail). Both are 0-based, and the position is the
    deployment slot on the map, not the order the lobby lists players in.
    See roster.TEAM_OFF. Opens the game process read-only.
    """
    if sys.platform != "win32":
        return []
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return []
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return []
    try:
        # find_roster_array recognises the array's owner by its type and
        # returns the allocation, so the seats are simply walked. The regions
        # it looked in are kept so the next call is a glance, not a sweep. A
        # miss falls back to the full search.
        arr, bases = find_roster_array(pid, hints=_ARRAY_HINTS or None)
        if not arr and _ARRAY_HINTS:
            arr, bases = find_roster_array(pid, hints=set())
        if bases:
            _ARRAY_HINTS.clear()
            _ARRAY_HINTS.update(bases)
        return (lobby_seats_detail(LiveSource(h), arr or None) if detail
                else lobby_seats(LiveSource(h), arr or None))
    finally:
        k32.CloseHandle(h)


def read_capacity(pid=None):
    """(seats per alliance) as the host made the lobby, e.g. (3, 3), or None.

    The configured size, not the attendance: see roster.CAPACITY_AT for the
    difference and for why a hosting lobby nobody has joined yet gives None.
    Opens the game process read-only.
    """
    if sys.platform != "win32":
        return None
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return None
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        arr, bases = find_roster_array(pid, hints=_ARRAY_HINTS or None)
        if not arr and _ARRAY_HINTS:
            arr, bases = find_roster_array(pid, hints=set())
        if bases:
            _ARRAY_HINTS.clear()
            _ARRAY_HINTS.update(bases)
        return lobby_capacity(LiveSource(h), roster_owner()) if arr else None
    finally:
        k32.CloseHandle(h)


def read_armies(pid=None):
    """{name: army key} for the lobby now, or {}.

    A separate call rather than another field on read_seats because the army
    reading is unconfirmed (see roster.ARMY_OFF); failing here costs the army
    and nothing else. Opens the game process read-only.
    """
    if sys.platform != "win32":
        return {}
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return {}
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return {}
    try:
        arr, bases = find_roster_array(pid, hints=_ARRAY_HINTS or None)
        if not arr and _ARRAY_HINTS:
            arr, bases = find_roster_array(pid, hints=set())
        if bases:
            _ARRAY_HINTS.clear()
            _ARRAY_HINTS.update(bases)
        return lobby_armies(LiveSource(h), arr or None)
    finally:
        k32.CloseHandle(h)


def main():
    """Command-line entry: print the lobby roster, once or repeatedly."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default="jojo_mcbean",
                    help="the local player's name; the search starts from it")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()

    while True:
        t0 = time.time()
        names = read_roster(args.local)
        # The size the host made the lobby, from the game's own field. Blank
        # when it cannot be read. See roster.CAPACITY_AT.
        cap = read_capacity()
        size = f"lobby {cap[0]}v{cap[1]}  " if cap else ""
        print(f"\n--- {time.strftime('%H:%M:%S')}  {len(names)} players  "
              f"{size}{(time.time() - t0) * 1000:.0f} ms")
        for n, sid, team, pos in read_seats():
            where = ("?" if team is None
                     else f"alliance {team + 1} seat {pos + 1}")
            print(f"   {n:34s} {sid}  {where}")
        if not names:
            print("   no lobby open")
        if not args.watch:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
