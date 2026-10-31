"""Decode the NTW3 lobby roster from the game's memory.

The single implementation, used by the live client and by the tests. It runs
against a live process or a saved capture; the only difference is where the
bytes come from, which is what the two MemorySource classes hide.

What it touches. This module opens no process itself. LiveSource reads through
a handle the caller opened (see live_seats.py) using ReadProcessMemory only;
nothing is ever written to the game. DumpSource opens a capture file on disk
read-only, at a path the caller supplies (test captures). What is decoded is
the lobby's seat array: each player's display name, SteamID64, alliance and
position, army preset, and the lobby's configured size. Nothing is written to
disk or sent anywhere; results are returned to the caller.

# The algorithm

Seats are records of 0x60 bytes laid out consecutively:

    X - 0x3c    SteamID64: a 32-bit account id, then 01 00 10 01
    X + 0x00    pointer -> the player's name, UTF-16LE
    X + 0x08    pointer -> their army preset, 'ntw3_tow_b03_x8_042'

The SteamID is the anchor. Its top four bytes are the same for every
individual account, so it can be found without knowing anything about the
session, and it identifies the record exactly. Everything else follows from
it. Scanning for name-shaped strings, or for proximity to lobby widgets, was
tried first and could not separate the current roster from stale copies; a
SteamID is a value rather than an address, so it survives the game moving.
"""

import re
import struct

STRIDE = 0x60           # the player array's pitch
MAX_SEATS = 24
#: The allocation header in front of the first seat record.
ARRAY_FIRST = 8
MAX_NAME = 40

#: The array is a fixed block of eight slots, and the dword straight after
#: the eighth is how many of them the game considers taken. A player who
#: leaves or moves seat leaves their record behind, structurally identical to
#: a live one; the count is what says it is dead, and live records are always
#: packed in front of dead ones. CPU seats have no record here at all.
ARRAY_SLOTS = 8
COUNT_OFF = ARRAY_FIRST + ARRAY_SLOTS * STRIDE
#: The owner's type tag, four bytes past the pointer to the array (see
#: lobby_memory.ROSTER_OWNER_TAG). Lets a capture find the array without
#: knowing where the module was loaded.
OWNER_TAG = 0x36
OWNER_ARRAY_AT = -0x1C
#: From the owner to a pointer whose target starts with two dwords: how many
#: seats each alliance has, as the host set it when creating the game (3 and 3
#: for a 3v3). Distinct from COUNT_OFF, which is how many people are present.
#: A lobby being hosted before anyone joins reads junk here, so the values are
#: believed only when both are 1..MAX_PER_ALLIANCE.
CAPACITY_AT = -0x0C
MAX_PER_ALLIANCE = 4

TAG_VALUE = 0x014E014C  # first field of a rendered UI object
TAG = struct.pack("<I", TAG_VALUE)

# Where the array has been in every capture so far, about 3% of the process.
# Looked at first; the whole space is still tried on a miss.
ARRAY_WINDOW = (0x3C000000, 0x3E000000)

FILENAME_RE = re.compile(r"\.(pack|png|tga|dds|lua|xml|twui|mp3|wav)$", re.I)


# --------------------------------------------------------------------------
# Memory sources


class DumpSource:
    """A capture file on disk, opened read-only and addressed as if it were
    the process."""

    def __init__(self, path):
        self.f = open(path, "rb")
        self.idx = []
        while True:
            hdr = self.f.read(16)
            if len(hdr) < 16:
                break
            base, size = struct.unpack("<QQ", hdr)
            self.idx.append((base, size, self.f.tell()))
            self.f.seek(size, 1)

    def read(self, addr, n):
        """Up to n bytes at addr from the capture, or None if not captured."""
        for base, size, off in self.idx:
            if base <= addr < base + size:
                self.f.seek(off + (addr - base))
                return self.f.read(min(n, size - (addr - base)))
        return None

    def regions(self, window=None, only=None):
        """Yield (base, bytes) for each captured region, optionally filtered."""
        for base, size, off in self.idx:
            if window and not (window[0] <= base < window[1]):
                continue
            if only is not None and base not in only:
                continue
            self.f.seek(off)
            yield base, self.f.read(size)

    def close(self):
        """Close the capture file."""
        self.f.close()


class LiveSource:
    """The running game, read through a handle the caller opened. Read-only:
    ReadProcessMemory is the only call made on the handle."""

    def __init__(self, handle):
        import ctypes
        from lobby_memory import k32
        self.ctypes, self.k32, self.h = ctypes, k32, handle

    def read(self, addr, n):
        """Up to n bytes at addr via ReadProcessMemory, or None on failure."""
        buf = self.ctypes.create_string_buffer(n)
        got = self.ctypes.c_size_t(0)
        if not self.k32.ReadProcessMemory(
                self.h, self.ctypes.c_void_p(addr), buf,
                self.ctypes.c_size_t(n), self.ctypes.byref(got)):
            return None
        return buf.raw[:got.value]

    def regions(self, window=None, only=None):
        """Yield (base, bytes) for each readable committed region of the
        process, optionally filtered by window or base set."""
        from lobby_memory import _regions, _read
        for base, size in _regions(self.h, max_region=256 << 20):
            if window and not (window[0] <= base < window[1]):
                continue
            if only is not None and base not in only:
                continue
            d = _read(self.h, base, size)
            if d:
                yield base, d


# --------------------------------------------------------------------------
# Decoding


def decode_name(raw):
    """Name bytes -> (text, was_widened), or (None, False).

    The engine does not store names as UTF-16. It stores the name's UTF-8
    bytes, each widened to 16 bits through a signed char, so 0xF0 arrives as
    0xFFF0. Narrowing every unit back to its low byte and reading the result
    as UTF-8 recovers the real name. Plain ASCII reads the same either way.

    Validity decides between the two readings: random bytes almost never form
    valid UTF-8, and a genuine UTF-16 name (an accented Latin-1 character as
    one unit) does not narrow to valid UTF-8, so it falls through to the
    UTF-16 branch.
    """
    if len(raw) < 2:
        return None, False
    units = struct.unpack(f"<{len(raw) // 2}H", raw[:len(raw) // 2 * 2])

    # A unit above 0xFF that is not sign extension cannot be a widened byte.
    if all(u <= 0xFF or (u & 0xFF00) == 0xFF00 for u in units):
        narrowed = bytes(u & 0xFF for u in units)
        try:
            s = narrowed.decode("utf-8")
            # Only claim "widened" when it mattered. An ASCII name decodes
            # the same either way and should keep the Latin check.
            return s, any(b >= 0x80 for b in narrowed)
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-16le"), False
    except UnicodeDecodeError:
        return None, False


def name_at(mem, p, min_len=2, lenient=False):
    """The player name at p, or None.

    `lenient` is for the seat-array walk, where the SteamID beside the pointer
    already proves the record is a player: a name that decodes as valid
    widened UTF-8 is then accepted even if nothing in it is printable (some
    players are named entirely in invisible Unicode characters).

    min_len is 2 for a scan, where a lone character is usually noise. The
    seat-array walk passes 1, because real players are called "0".

    Traps this guards against: the UTF-16 terminator must be found on an even
    offset, since the last character's high byte pairs with the terminator's
    low byte; ASCII misread as UTF-16 decodes as printable CJK, so names must
    be mostly Latin unless UTF-8 already validated them; and filenames decode
    perfectly well, so they are rejected by extension.
    """
    for delta in (0, -2, 2):
        raw = mem.read(p + delta, 128)
        if not raw:
            continue
        end = -1
        for q in range(0, len(raw) - 1, 2):
            if raw[q] == 0 and raw[q + 1] == 0:
                end = q
                break
        if end < 2 * min_len:
            continue
        s, widened = decode_name(raw[:end])
        if s is None:
            continue
        if not (min_len <= len(s) <= MAX_NAME):
            continue
        if not s.isprintable() and not (lenient and widened):
            continue
        # The Latin ratio rejects ASCII misread as UTF-16. A widened-UTF-8
        # name has already proved itself by decoding and may be non-Latin.
        if not widened and sum(1 for c in s if ord(c) < 0x250) < 0.8 * len(s):
            continue
        if FILENAME_RE.search(s):
            continue
        return s
    return None


def read_array(mem, base):
    """Names in the array at base, ending where a slot stops decoding."""
    out = []
    for k in range(MAX_SEATS):
        raw = mem.read(base + k * STRIDE, 4)
        if not raw or len(raw) < 4:
            break
        v = struct.unpack("<I", raw)[0]
        if v < 0x10000:
            break
        n = name_at(mem, v)
        if not n:
            break
        out.append(n)
    return out


def find_all(hay, needle, limit=1 << 30):
    """Every offset of needle in hay, up to limit."""
    out, i = [], hay.find(needle)
    while i >= 0 and len(out) < limit:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


# --------------------------------------------------------------------------
# Reading the roster

SID_HI = struct.pack("<I", 0x01100001)      # top half of every individual SteamID64
SID_BASE = 0x0110000100000000
STEAM_EPOCH = 76561197960265728
NAME_OFF = 0x3C                    # the name pointer, relative to the id
MAX_GAP = 4                        # empty seats allowed between neighbours


def seat_records(mem, array=None):
    """[(address, steamid64, name)] for every SteamID with a name beside it.

    `array` is the seat array's allocation when the caller already knows it
    (see lobby_memory.find_roster_array). Given one, the records are walked
    directly and no regions are read, which is milliseconds instead of a
    sweep. Without it, every readable region is searched for the SteamID
    high dword, so a build whose owner signature no longer holds still reads
    its roster.
    """
    if array:
        return _records_from_array(mem, array)
    out = []
    for base, d in mem.regions():
        i = d.find(SID_HI)
        while i >= 0:
            a = base + i - 4
            if i >= 4 and a % 4 == 0:
                acct = struct.unpack_from("<Q", d, i - 4)[0] - SID_BASE
                if 0 < acct < (1 << 32):
                    p = mem.read(a + NAME_OFF, 4)
                    if p and len(p) == 4:
                        v = struct.unpack("<I", p)[0]
                        if v > 0x10000:
                            nm = name_at(mem, v)
                            if nm:
                                out.append((a, STEAM_EPOCH + acct, nm))
            i = d.find(SID_HI, i + 1)
    out.sort()
    return out


def _slot_kind(mem, a):
    """What occupies the 0x60 bytes at `a`.

    'seat'  a SteamID64 in the first eight bytes
    'empty' entirely zero: an unoccupied slot in the array
    'head'  the first eight bytes zero, the rest not: a vacated slot
    'other' anything else, which means this is not the seat array
    """
    raw = mem.read(a, STRIDE)
    if not raw or len(raw) < STRIDE:
        return "other"
    if raw == bytes(STRIDE):
        return "empty"
    acct = struct.unpack_from("<Q", raw, 0)[0] - SID_BASE
    if 0 < acct < (1 << 32):
        return "seat"
    return "head" if raw[:8] == bytes(8) else "other"


def anchored(mem, a, limit=16):
    """Does the run containing `a` begin at a zeroed slot?

    The seat array is allocated as a block of 0x60 slots with the unused ones
    zeroed, so walking backwards through seats and empties from any real seat
    reaches a zeroed slot. A SteamID that merely happens to have a string
    pointer 0x3c after it is surrounded by unrelated data and the walk stops
    at once. This is what separates real seats from phantoms; asset-name
    blacklists and "must have a neighbour" rules both failed.
    """
    for _ in range(limit):
        a -= STRIDE
        kind = _slot_kind(mem, a)
        if kind == "empty":
            return True
        if kind not in ("seat", "head"):
            return False
    return False


#: Byte in the seat record holding the lobby's own view of who is where.
#: Two fields packed into one byte:
#:     bit 0        which alliance, 0 or 1
#:     bits 2-3     the player's position within that alliance, 0-3
#:
#: The position is the deployment slot (which box on the map the player forms
#: up in). It is not join order and not the order the lobby lists people in,
#: both of which were believed at one point and disproved by captures. Found
#: by testing every bit position in the record against a lobby read off the
#: screen; checked against every capture in test/, where 40 of the 43 with
#: seats decode to alliances whose positions are exactly 0..n-1.
TEAM_OFF = 0x54


def team_of(mem, a):
    """(alliance, position) for the seat at `a`, or (None, None).

    Both are 0-based. Alliance 0 is drawn first, as "Alliance 1". The split
    of the byte is an inference, not something the game documents: it can
    express only two alliances and four slots, and bit 1 is ignored. A fifth
    slot or a third alliance would wrap silently, which is why seat_byte
    exists and the raw value is reported for the server to check against the
    replay.
    """
    b = seat_byte(mem, a)
    if b is None:
        return None, None
    return b & 1, (b >> 2) & 3


def seat_byte(mem, a):
    """The raw byte the alliance and position are decoded from, or None.

    Reported alongside the decoded values so a wrong reading can be diagnosed
    from the archive, against the replay's record of where people deployed.
    """
    b = mem.read(a + TEAM_OFF, 1)
    return b[0] if b else None


#: The army pointer, relative to the seat's SteamID.
#:
#: NOT CONFIRMED against a lobby screen. A string sits here in every capture
#: ('aaa_lordz', 'ntw3_tow_a04_x8_005', 'ntw3_hre'), and one has an obvious
#: counterpart among the army labels replays record. Suggestive, not proof,
#: so nothing depends on it: the value is reported beside the seat and the
#: server stores it for checking against the replay. Whether this is one
#: field holding both preset lists and factions is an open question.
ARMY_OFF = 0x20


def army_at(mem, a):
    """The army key for the seat at `a`, or None.

    The string is wide, like the player name beside it; name_at already
    handles that and the other string traps, so this only decides whether
    what came back looks like a key.
    """
    p = mem.read(a + ARMY_OFF, 4)
    if not p or len(p) < 4:
        return None
    addr = int.from_bytes(p, "little")
    # A 32-bit process: below a megabyte is a number, above 2 GB is not a heap
    # pointer.
    if not (0x00100000 < addr < 0x7FFF0000):
        return None
    text = name_at(mem, addr)
    if not text or len(text) > 48:
        return None
    # The keys seen are lower-case words, digits and underscores. Anything
    # else is a different string that happens to live at this offset.
    if not all(c.isalnum() or c in "_-." for c in text):
        return None
    return text


def lobby_armies(mem, array=None):
    """{name: army key} for the lobby now.

    A separate call rather than another field on lobby_seats_detail, so an
    unconfirmed reading does not widen a tuple that is unpacked in several
    places.
    """
    out = {}
    for a, sid, name in seat_records(mem, array):
        if not anchored(mem, a):
            continue
        army = army_at(mem, a)
        if army:
            out[name] = army
    return out


def seat_count(mem, array):
    """How many seats the game says are taken, or None if it cannot be read.

    See COUNT_OFF. A value above the slot count is not a count and is
    treated as unreadable.
    """
    raw = mem.read(array + COUNT_OFF, 4)
    if not raw or len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    return n if n <= ARRAY_SLOTS else None


def find_owner(mem, array):
    """The owner object holding `array`, or 0. A sweep, for captures.

    The holder of a pointer to the allocation with the type tag four bytes
    past that pointer. Live, find_roster_array already has it (see
    lobby_memory.roster_owner).
    """
    pat = struct.pack("<I", array)
    for base, d in mem.regions():
        for i in find_all(d, pat):
            loc = base + i
            if loc % 4 != 0:
                continue
            tag = mem.read(loc - OWNER_ARRAY_AT + 4, 4)
            if tag and struct.unpack("<I", tag)[0] == OWNER_TAG:
                return loc - OWNER_ARRAY_AT
    return 0


def find_array(mem):
    """The seat array's allocation in a capture, or 0.

    The live reader recognises the array's owner by its vtable, which needs
    the module base, and older captures do not record one. This goes the
    other way: from any anchored seat record, walk down to the lowest slot of
    its run, then ask who holds a pointer to the allocation that would start
    there (find_owner). Two allocations are tried because the slot below
    record 0 reads as a vacated seat rather than an empty one, so the walk
    overshoots by one stride.
    """
    tried = set()
    for a, _sid, _nm in seat_records(mem):
        if not anchored(mem, a):
            continue
        low = a
        for _ in range(ARRAY_SLOTS + 1):
            if _slot_kind(mem, low - STRIDE) in ("seat", "head"):
                low -= STRIDE
            else:
                break
        for rec0 in (low + STRIDE, low):
            alloc = rec0 - ARRAY_FIRST
            if alloc in tried:
                continue
            tried.add(alloc)
            if seat_count(mem, alloc) is None:
                continue
            if find_owner(mem, alloc):
                return alloc
    return 0


def lobby_capacity(mem, owner):
    """(seats in alliance 1, seats in alliance 2) as the host set them, or None.

    See CAPACITY_AT. None when the owner is unknown or the values are not
    believable, as when hosting before anyone has joined.
    """
    if not owner:
        return None
    p = mem.read(owner + CAPACITY_AT, 4)
    if not p or len(p) < 4:
        return None
    p = struct.unpack("<I", p)[0]
    if not (0x10000 < p < 0x7FFF0000):
        return None
    raw = mem.read(p, 8)
    if not raw or len(raw) < 8:
        return None
    a, b = struct.unpack("<II", raw)
    if not (1 <= a <= MAX_PER_ALLIANCE and 1 <= b <= MAX_PER_ALLIANCE):
        return None
    return a, b


def _records_from_array(mem, array):
    """Walk the seat array directly. [(address, steamid64, name)].

    Stops after the game's own count of taken seats (see COUNT_OFF) so a
    record left behind by someone who moved or left is never read as a
    player. Without a readable count every slot is walked.
    """
    out = []
    want = seat_count(mem, array)
    taken = 0
    for n in range(MAX_SEATS):
        if want is not None and taken >= want:
            break
        a = array + ARRAY_FIRST + n * STRIDE
        raw = mem.read(a, 8)
        if not raw or len(raw) < 8:
            break
        sid = struct.unpack("<Q", raw)[0]
        # A vacated slot does not end the array: the game keeps the hole
        # rather than closing it. See _slot_kind.
        if not (STEAM_EPOCH <= sid < STEAM_EPOCH + (1 << 32)):
            continue
        taken += 1
        p = mem.read(a + NAME_OFF, 4)
        if not p or len(p) != 4:
            continue
        v = struct.unpack("<I", p)[0]
        if v > 0x10000:
            nm = name_at(mem, v, min_len=1, lenient=True)
            if nm:
                out.append((a, sid, nm))
            else:
                # A seat with a SteamID is a player whatever its name reads
                # as; dropping it would hide the account from the ladder.
                out.append((a, sid, f"[{sid}]"))
    return out


def lobby_seats(mem, array=None):
    """[(name, steamid64, alliance, position)] for the lobby now.

    Same players as lobby_roster, with where each one is sitting. `array` is
    passed through to seat_records.
    """
    hits = seat_records(mem, array)
    seen, out = set(), []
    for a, sid, name in hits:
        if sid in seen or not anchored(mem, a):
            continue
        seen.add(sid)
        team, pos = team_of(mem, a)
        out.append((name, sid, team, pos))
    return out


def lobby_seats_detail(mem, array=None):
    """As lobby_seats, plus the raw byte and the seat's index in the array.

    [(name, steamid64, alliance, position, raw_byte, index)]

    Kept separate from lobby_seats so the shape everything already unpacks
    does not change. The two extra values make a wrong reading diagnosable:
    the raw byte is what the game stored, and the index is where the seat sat
    in the array, which is not the deployment slot.
    """
    hits = seat_records(mem, array)
    seen, out = set(), []
    for idx, (a, sid, name) in enumerate(hits):
        if sid in seen or not anchored(mem, a):
            continue
        seen.add(sid)
        team, pos = team_of(mem, a)
        out.append((name, sid, team, pos, seat_byte(mem, a),
                    (a - array - ARRAY_FIRST) // STRIDE if array else idx))
    return out


def seat_faults(seats):
    """Alliances whose seat numbers are not 1..n. Empty when the lobby is fine.

    A healthy lobby gives each alliance a run of seats with no holes, and
    every capture in test/ does. Against a capture, a gap means the roster
    was misread. Against a live lobby whose roster is otherwise stable, it
    means players left and the game kept their slots as vacated; such a lobby
    may refuse to start, which is worth telling the player. Duplicates count
    as a fault too: two players cannot deploy in one box.
    """
    by = {}
    # Sliced, not unpacked: lobby_seats gives four fields and
    # lobby_seats_detail gives six, and this is handed either.
    for seat in seats:
        team, pos = seat[2], seat[3]
        if team is None or pos is None:
            continue
        by.setdefault(team, []).append(pos)
    bad = []
    for team, taken in by.items():
        if sorted(taken) != list(range(len(taken))):
            bad.append(team)
    return sorted(bad)


def lobby_roster(mem, local=None, array=None):
    """[(name, steamid64)] for the players in the lobby now.

    `local` is accepted and ignored; the search needs no seed. `array` is
    passed through to seat_records; with it, the game's own seat count
    applies.

    Your own seat survives leaving the lobby for a while, and the residual
    record is structurally identical to a real one, so you keep seeing
    yourself on the menu screen. That is accepted rather than guessed around.
    """
    # Every candidate is judged on its own by whether it sits in an anchored
    # run. Grouping by stride and taking the largest group was tried and
    # silently dropped half a lobby whose alliances sat in separate blocks.
    hits = seat_records(mem, array)
    seen, out = set(), []
    for _a, sid, name in hits:
        if sid in seen or not anchored(mem, _a):
            continue
        seen.add(sid)
        out.append((name, sid))
    return out


def roster_of_dump(path, local=None):
    """The roster in a capture file at `path`. For tests and offline analysis."""
    mem = DumpSource(path)
    try:
        # The array is found through its owner so the seat count applies,
        # exactly as the live reader does it. A miss falls back to the sweep.
        return lobby_roster(mem, local, find_array(mem) or None)
    finally:
        mem.close()
