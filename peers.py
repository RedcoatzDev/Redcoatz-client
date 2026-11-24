"""Who is still in the battle.

The game keeps its own count of the players in a battle. This reads that
count, read only, and says which of the lobby's players the game no longer
counts. Nothing is written to the game: the panel uses it to say "X has left
the game" and to take X off its list.

    b = Battle(steam_ids, me)
    b.gone(pid, "full")      -> the steam ids the game has stopped counting

Three depths of check, cheapest first: "spots" looks again at the places
found before (milliseconds), "near" searches the memory around them, "full"
searches everything (a few seconds, so seldom, and never on the Tk thread).
"""

import struct

from lobby_memory import (k32, _regions, _read, PROCESS_QUERY_INFORMATION,
                          PROCESS_VM_READ)

#: Slots in one of the game's player lists.
SLOTS = 8
#: How many lists must agree before a player counts as gone.
AGREE = 3
_MARK = bytes((0x01, 0x00, 0x10, 0x01))


class Battle:
    """One battle's players, and where the game keeps them."""

    def __init__(self, sids, me=None):
        self.sids = [int(s) for s in sids]
        self.me = int(me) if me else None
        self._pats = {struct.pack("<Q", s): s for s in self.sids}
        # The orders the game keeps the players in, learnt on the first
        # search while everybody is still there. Empty until then.
        self.orders = []
        self._spots = {}                 # address -> which order
        self._near = set()

    @property
    def order(self):
        return self.orders[0] if self.orders else None

    # ---- reading -----------------------------------------------------------

    def _slots(self, d, p, n):
        """The n slots at p: a player's id, 0 for empty, -1 for anything else."""
        if p < 0 or p + 8 * n > len(d):
            return None
        return tuple(self._pats.get(d[p + 8 * i:p + 8 * i + 8],
                                    0 if d[p + 8 * i:p + 8 * i + 8] == b"\0" * 8 else -1)
                     for i in range(n))

    def _judge(self, slots, order):
        """Who this list says has gone, or None if it is not one of ours.

        The game shows a departure in one of two ways: the player's place is
        emptied, or the players after them move up one and the end of the
        list is left as it was.
        """
        if slots is None or -1 in slots:
            return None
        want = [s for s in order if s]
        if 0 in slots or 0 in order:
            live = 0
            for got, was in zip(slots, order):
                if got == was:
                    live += 1 if was else 0
                elif got != 0:
                    return None
            if live < 2:
                return None
            gone = {was for got, was in zip(slots, order) if was and not got}
            # Without knowing who we are the first place is not judged: a
            # second list the game keeps can make the first look gone.
            if self.me not in want:
                gone.discard(want[0])
        else:
            # Moved up: what is still live is the run before the first
            # repeat, and it keeps the order it had.
            head, seen = [], set()
            for s in slots:
                if s in seen:
                    break
                seen.add(s)
                head.append(s)
            it = iter(want)
            if not all(s in it for s in head):
                return None
            gone = set(want) - set(head)
        # We are always in a list we follow.
        if self.me in gone:
            return None
        return gone

    def _search(self, h, regions):
        """Find the lists in `regions` (all memory if None)."""
        learnt = {}
        found, near = {}, set()
        for base, size in (regions if regions is not None
                           else _regions(h, max_region=256 << 20)):
            d = _read(h, base, size)
            if not d:
                continue
            hi = d.find(_MARK)
            while hi != -1:
                p = hi - 4
                hi = d.find(_MARK, hi + 4)
                sid = self._pats.get(d[p:p + 8]) if p >= 0 else None
                if sid is None:
                    continue
                first = not (p >= 8 and d[p - 8:p] in self._pats)
                if not self.orders:
                    # Learning: a run that starts here and names everybody
                    # exactly once. As far as the last player: what follows
                    # a short list is not ours to judge.
                    if not first:
                        continue
                    slots = self._slots(d, p, SLOTS) or ()
                    if -1 in slots:
                        slots = slots[:slots.index(-1)]
                    while slots and not slots[-1]:
                        slots = slots[:-1]
                    if sorted(s for s in slots if s) == sorted(self.sids):
                        learnt.setdefault(slots, []).append((base + p, base, size))
                    continue
                for k, order in enumerate(self.orders):
                    # A list with this player in their own place, and, where
                    # a run starts here, one that has moved up.
                    starts = {p - 8 * order.index(sid)}
                    if first:
                        starts.add(p)
                    for q in starts:
                        if base + q in found:
                            continue
                        if self._judge(self._slots(d, q, len(order)), order) is not None:
                            found[base + q] = k
                            near.add((base, size))
        if not self.orders and learnt:
            ranked = sorted(learnt.items(), key=lambda kv: -len(kv[1]))
            keep = [kv for kv in ranked if len(kv[1]) >= AGREE] or ranked[:1]
            for k, (order, where) in enumerate(keep):
                self.orders.append(order)
                for a, b, n in where:
                    found[a] = k
                    near.add((b, n))
        return found, near

    def check(self, pid, depth="spots"):
        """{steam_id: lists that say they are gone}, or None if unreadable."""
        if len(self.sids) < 2 or not pid:
            return None
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return None
        try:
            if not self.orders:
                depth = "full"
            if depth == "full":
                found, near = self._search(h, None)
                self._spots.update(found)
                self._near |= near
            elif depth == "near" and self._near:
                found, _ = self._search(h, sorted(self._near))
                self._spots.update(found)
            if not self.orders:
                return None
            votes = {}
            for a, k in list(self._spots.items()):
                order = self.orders[k]
                gone = self._judge(self._slots(_read(h, a, 8 * len(order)), 0, len(order)), order)
                if gone is None:
                    del self._spots[a]
                    continue
                for s in gone:
                    votes[s] = votes.get(s, 0) + 1
        finally:
            k32.CloseHandle(h)
        return votes

    def gone(self, pid, depth="spots"):
        """The players enough of the game's lists agree have left."""
        votes = self.check(pid, depth)
        if votes is None:
            return None
        return {s for s, n in votes.items() if n >= AGREE}
