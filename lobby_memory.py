"""Read the NTW3 lobby and battle state out of the running game's memory.

    python lobby_memory.py            print who is in the lobby
    python lobby_memory.py --watch    print whenever it changes

This is the low-level memory reader behind the overlay. It finds the game,
enumerates its memory regions, and searches them for a small set of facts:
who is in the lobby (display names and SteamID64s, plus "CPU n" seats), which
screen is up (lobby, battle, results), which map the lobby is on, whether the
Save Replay button or the results table is showing, and whether the game has
announced that the host left, the player dropped, or a desync occurred.

What it touches, stated plainly for anyone deciding whether to trust it:

* Process: the first process whose executable name contains "napoleon",
  found with CreateToolhelp32Snapshot (find_game). The module base of
  napoleon.exe is looked up with EnumProcessModules (module_base).
* Access: every handle is opened with PROCESS_QUERY_INFORMATION |
  PROCESS_VM_READ and closed before the function returns. The only calls
  made on a handle are VirtualQueryEx (list regions), ReadProcessMemory and
  EnumProcessModules / GetModuleBaseNameW. There is no WriteProcessMemory,
  no VM_WRITE or VM_OPERATION right, and nothing in this module or in
  roster.py / live_seats.py writes to the game's memory.
* Disk: nothing is written. No file is read either.
* Network: nothing is sent from here. Readings are returned to the caller,
  normally ladder_overlay.py, which shows them in its panel and includes
  the roster, map and outcome in the game start and game end reports it
  POSTs to the ladder server. Small caches (regions that held something
  last time, the cached map label) live in module globals only.

Two costs of memory scanning are worth stating. Antivirus heuristics dislike
OpenProcess against a running game, so this may need an exclusion. And the
offsets marked RVA below were measured against one build of Napoleon.exe;
every reader that depends on them returns "unreadable" rather than a guess
when they do not hold, and the string-pattern fallbacks take over.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import re
import struct

import mapnames
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = (0x02, 0x04, 0x20, 0x40, 0x80)

# "<name> <steamid64 hex>": the game keeps each lobby member as this plain
# string. Every individual SteamID64 is 0x0110000100000000 plus an account
# id, so in hex they all begin "1100001", which is specific enough to find
# without false positives. The name half accepts any non-control byte and is
# validated as UTF-8 afterwards; a printable-ASCII range lost the first
# character of names like "¤sprucedoors".
ROSTER_RE = re.compile(rb"([^\x00-\x1f\x7f]{2,48}) (1100001[0-9a-f]{8,9})\b")
STEAMID64_BASE = 76561197960265728

# AI seats ("CPU 1".."CPU 4") carry no SteamID. They are found through the
# structure of the lobby's rows (see ROW_STRIDE) rather than by searching for
# the literal strings, which freed memory keeps long after the seat is gone.
CPU_RE = re.compile(r"^CPU \d+$")
INCLUDE_AI_DEFAULT = True

# Live lobby rows, found by structure rather than by text.
#
# The lobby screen keeps its widget text in a table of UTF-16 strings on a
# 40-byte stride. One row looks like this, with the seat's name 15 slots ahead
# of the rank/icon pair:
#
#     slot -15   "CPU 1"          <- the occupant
#     slot -14.. "Item Group"     <- filler
#     slot   0   "player_rank"
#     slot  +1   "leader_icon"
#
# player_rank immediately followed by leader_icon occurs only in a real row;
# the two names alone are shared widget templates and appear dozens of times.
# Loose "CPU 2" strings left in freed memory have no row around them, which is
# what stops a lobby of one bot reporting four.
ROW_STRIDE = 40
ROW_NAME_SLOT = -15
# Slot values that mean "this row is not an occupied seat".
ROW_PLACEHOLDERS = {"", "Unused_key", "row_example", "Player name",
                    "Item Group", "title_tx", "border_top", "mini_flag",
                    "popup_list", "Kick player"}
# Strings that mark the region holding the lobby screen.
LOBBY_MARKERS = ("Kick player", "team_1", "Ready!")


class MBI(ctypes.Structure):
    """MEMORY_BASIC_INFORMATION, as filled in by VirtualQueryEx."""
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__a1", wt.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__a2", wt.DWORD),
    ]


TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260


class PROCESSENTRY32(ctypes.Structure):
    """One entry of a Toolhelp process snapshot."""
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * MAX_PATH),
    ]


def find_game():
    """(pid, exe) of the first process whose name contains "napoleon", or
    (None, None).

    Uses the process-snapshot API rather than shelling out to tasklist, which
    pops a console window from a GUI process on every poll.
    """
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return None, None
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not k32.Process32First(snap, ctypes.byref(pe)):
            return None, None
        while True:
            name = pe.szExeFile.decode("mbcs", "replace")
            if "napoleon" in name.lower():
                return pe.th32ProcessID, name
            if not k32.Process32Next(snap, ctypes.byref(pe)):
                return None, None
    finally:
        k32.CloseHandle(snap)


def _regions(h, max_region=16 << 20):
    """Yield (base, size) of every committed, readable region of the process
    behind handle h, up to max_region bytes each. Uses VirtualQueryEx only."""
    addr = 0
    mbi = MBI()
    while addr < (1 << 47):
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)):
            break
        base, size, prot = mbi.BaseAddress, mbi.RegionSize, mbi.Protect
        if size == 0:
            break
        if (mbi.State == MEM_COMMIT and not (prot & PAGE_GUARD)
                and prot != PAGE_NOACCESS and (prot & 0xFF) in READABLE
                and size <= max_region):
            yield base, size
        addr = base + size


def _read(h, addr, size):
    """Read `size` bytes at addr through handle h with ReadProcessMemory.
    b"" on failure."""
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf,
                                 ctypes.c_size_t(size), ctypes.byref(got)):
        return b""
    return buf.raw[:got.value]


def _slot(d, off):
    """The UTF-16 string starting at a 40-byte slot boundary."""
    if off < 0 or off + ROW_STRIDE > len(d):
        return None
    m = re.match(rb"(?:[\x20-\x7e]\x00){1,20}", d[off:off + ROW_STRIDE])
    return m.group().decode("utf-16-le").rstrip("\x00").strip() if m else ""


def live_seats(d):
    """Occupants of real lobby rows in one memory region.

    Anchors on player_rank immediately followed by leader_icon, which only
    happens inside an actual row, then reads the name slot. Placeholder rows
    (the header, the empty-seat template, the row template) are dropped.
    """
    out = []
    pr = "player_rank".encode("utf-16-le")
    start = 0
    while True:
        i = d.find(pr, start)
        if i < 0:
            return out
        start = i + 1
        if _slot(d, i + ROW_STRIDE) != "leader_icon":
            continue
        name = _slot(d, i + ROW_NAME_SLOT * ROW_STRIDE)
        if name and name not in ROW_PLACEHOLDERS:
            out.append(name)


# Fallback screen test, by the count of a lobby widget's name. "leader_icon"
# appears about twenty times while a lobby screen is up and once during a
# battle or on the results screen, so the threshold sits in a gap. The test
# looks for the LOBBY on purpose: everything a battle allocates stays
# allocated and would report a battle long after it ended. in_lobby, which
# walks the widget table, is preferred when it can answer.
LOBBY_WIDGET = b"leader_icon"
LOBBY_WIDGET_MIN = 10

STATE_LOBBY = "lobby"
STATE_BATTLE = "battle"     # or the results screen; the widgets are the same
STATE_UNKNOWN = "unknown"   # game not running, or not readable

# The Save Replay button, counted so the panel can reappear while it is on
# screen. The count cannot use a fixed threshold: it reads 0 in the lobby,
# about 11 on the results screen, and stays raised afterwards because the
# memory is not freed. The signal is therefore a RISE of at least SAVE_JUMP
# above the baseline sampled while the battle runs; if it never rises the
# panel simply restores when the lobby returns.
SAVE_WIDGET = b"save_replay"
SAVE_JUMP = 6

#: Thresholds for the same strings counted the cheap way, in regions that are
#: new since the battle began (scan_battle_signals). Fewer copies are visible
#: there than in a full scan, so the full-scan thresholds do not carry over.
#: Each sits below the smallest showing measured in captures and above the
#: resting count in new regions, which is zero.
SAVE_NEW_MIN = 3
HOST_LEFT_NEW = 3
DROPPED_NEW = 2

#: How long a newly appeared region keeps being re-read. Memory is allocated
#: before it is written, so a region read once at the moment it appears is
#: often still empty; filing it as seen on that basis delayed detection by
#: half a minute.
FRESH_SECONDS = 8.0

#: Which fresh regions are worth re-reading, and how much per pass. The text
#: lands in small regions (the largest seen holding save_replay was about
#: 3 MB), and the results screen commits over 200 MB of new regions, so a
#: size cap and a per-pass byte budget keep the half-second loop ahead.
FRESH_MAX_BYTES = 8 << 20
FRESH_BUDGET = 48 << 20

#: What the game puts on screen when the host goes, and when you personally
#: drop. Both strings sit in the localisation table from boot, so presence
#: proves nothing; the game duplicates a string while drawing it, so the
#: COUNT is the signal, and the two move independently. A host leaving writes
#: no replay and no desync file, so this string is the only trace such a
#: battle leaves. Thresholds sit nearer the resting count than the observed
#: peak because a false detection invents a host crash.
HOST_LEFT = "The host has left the game".encode("utf-16-le")
SELF_DROPPED = "You dropped from the game".encode("utf-16-le")

#: RVA of a pointer to the last string the game's UI rendered. Reading it
#: answers "what is the game showing" with one pointer read and one string
#: read. It is a render cache rather than a status field, so it churns; what
#: makes it usable is that the values that matter recur (a results screen
#: redraws its verdict every second or two) and a modal dialog stops anything
#: else being drawn. An RVA because the exe has ASLR.
STATUS_RVA = 0x14356F8

#: What the status text says, when it says something worth reporting.
STATUS_HOST_LEFT = "The host has left the game"
STATUS_DROPPED = "You dropped from the game"
STATUS_STEAM_LOST = "Connection to Steam was lost"
#: A desync, which the game announces without using the word (its string is
#: network_message_desynch). Separate from the desync log the client uploads:
#: the log says a battle desynchronised at some tick; this says the player
#: was told so and the battle ended there.
STATUS_DESYNC = "A synchronisation error has occurred"

#: A battle result always contains one of these words ("Decisive Victory",
#: "Close Defeat"). Matching the word avoids listing every adjective.
STATUS_OUTCOMES = ("Victory", "Defeat", "Draw")

_psapi = (ctypes.WinDLL("psapi", use_last_error=True)
          if sys.platform == "win32" else None)
#: pid -> module base, so the lookup is not repeated every poll.
_base_cache = {}


def module_base(pid, want="napoleon.exe"):
    """Load address of napoleon.exe in process pid, or None.

    Opens the process read-only and lists its modules with
    EnumProcessModules / GetModuleBaseNameW; the result is cached per pid.
    """
    if sys.platform != "win32" or not pid:
        return None
    if pid in _base_cache:
        return _base_cache[pid]
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        need = ctypes.c_ulong()
        arr = (ctypes.c_void_p * 1024)()
        if not _psapi.EnumProcessModules(h, ctypes.byref(arr),
                                         ctypes.sizeof(arr),
                                         ctypes.byref(need)):
            return None
        name = ctypes.create_unicode_buffer(260)
        for i in range(need.value // ctypes.sizeof(ctypes.c_void_p)):
            _psapi.GetModuleBaseNameW(h, arr[i], name, 260)
            if name.value.lower() == want:
                _base_cache[pid] = arr[i]
                return arr[i]
    finally:
        k32.CloseHandle(h)
    return None


def status_text(pid):
    """What the game's UI last rendered, or None.

    Opens the process read-only and follows the pointer at STATUS_RVA. None
    when the pointer does not lead anywhere sensible, which is the signal to
    fall back to scanning: the offset was measured against one build of
    Napoleon.exe.
    """
    base = module_base(pid)
    if not base:
        return None
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        raw = _read_at(h, base + STATUS_RVA, 4)
        if len(raw) < 4:
            return None
        p = struct.unpack("<I", raw)[0]
        if not (0x10000 <= p < 0x7FFF0000):
            return None
        s = _read_at(h, p, 400)
        if not s:
            return None
        # UTF-16, so the terminator only counts on an even offset: splitting
        # on any two zero bytes clipped the last character off every string.
        end = len(s)
        for i in range(0, len(s) - 1, 2):
            if s[i] == 0 and s[i + 1] == 0:
                end = i
                break
        return s[:end].decode("utf-16-le", "ignore")
    finally:
        k32.CloseHandle(h)


# ---- the widget tree -------------------------------------------------------
#
# The UI's live widgets, read structurally. The status pointer above holds
# only whatever was drawn last and is guarded, so it can read null for the
# whole end of a battle. A widget is state: it exists as long as the screen
# does, carries a name, and the name says what the text it displays means.
# Every live widget points at one shared type at +0xD0, so a search for that
# value finds all of them; the name follows at +0xF4 and the displayed text
# at +0x108. RVAs, resolved against the module base, because the exe has ASLR.
WIDGET_TYPE_RVA = 0x106D890
WIDGET_TYPE_AT, WIDGET_NAME_AT, WIDGET_TEXT_AT = 0xD0, 0xF4, 0x108

#: The end-of-battle screen, by the names of the widgets only it builds.
#: A fact about the screen, unlike SAVE_WIDGET's byte count, which has to
#: guess a threshold. Either name will do.
RESULTS_WIDGETS = ("button_dismiss_results", "button_save_replay")

#: What the game calls a result. Matched against widget TEXT, not names: the
#: outcome sits on a pool object that carries text and no name. Substrings,
#: because the game says "Close Defeat" as well as the bare word.
RESULT_WORDS = ("Victory", "Defeat", "Draw")

#: The text on the Save Replay button. The end-of-battle screen shows this
#: seconds before the byte count crosses its threshold.
SAVE_TEXT = "Save replay"

#: The title of the end-of-battle table, a different screen from the Save
#: Replay offer and the only one carrying the verdict; it appears about ten
#: seconds later, so a reader that stops at the first never sees the second.
RESULTS_TEXT = "Battle Results"

#: A widget name has to look like an identifier. The type search also matches
#: an interned string pool whose objects share the type but keep no name, so
#: their +0xF4 decodes as garbage; requiring an identifier drops them.
#: NUL is one zero byte, named so the escape survives editing.
NUL = bytes(1)

_IDENT = re.compile(rb"^[A-Za-z_][A-Za-z0-9_]{2,63}$")


def _utf16_at(h, p, n=120):
    """The UTF-16 string at p, read through handle h, or "".

    The terminator counts only on an even offset: the high byte of a final
    ASCII character and the first byte of the terminator look the same.
    """
    if not p:
        return ""
    raw = _read_at(h, p, n)
    for k in range(0, len(raw) - 1, 2):
        if raw[k] == 0 and raw[k + 1] == 0:
            raw = raw[:k]
            break
    t = raw.decode("utf-16-le", "ignore")
    return t if t.isprintable() else ""


#: The top-level widget table: an array of the widgets currently built.
#: `[base + WIDGET_TABLE_RVA]` points to the array; each slot is a widget
#: pointer. Found by a cross-run pointer scan and checked against captures
#: from four ASLR bases. A widget either is in the table or it is not, which
#: replaces thresholds on string counts.
WIDGET_TABLE_RVA = 0x1064488
WIDGET_TABLE_MAX = 512

#: The multiplayer lobby's own screen widget. Present in every lobby capture
#: and in none of battle, deployment, results, save screen or main menu.
LOBBY_SCREEN_WIDGET = "mp_battle3"

#: Widgets the battle builds and the lobby never does. A POSITIVE sighting:
#: the old test for a battle was an absence of lobby widgets, which a lobby
#: halfway through being rebuilt produces just as well, and that is where
#: phantom battle starts came from. Chosen by comparing widget tables of
#: battle captures against lobby captures; mp_battle3 does the reverse, so the
#: two tests can never both be true.
BATTLE_SCREEN_WIDGETS = ("radar_group", "victory_tickets_panel", "menu_bar",
                         "BattleUnitCard", "unit_cat_frame")


def top_widgets(pid, base=None):
    """[(index, address, name)] of the widgets currently on screen.

    Opens the process read-only and walks the widget table: one read for the
    table, then a few per slot. No scanning and no thresholds. Returns []
    when the game cannot be read.
    """
    if sys.platform != "win32" or not pid:
        return []
    base = base or module_base(pid)
    if not base:
        return []
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return []
    try:
        raw = _read_at(h, base + WIDGET_TABLE_RVA, 4)
        if len(raw) < 4:
            return []
        table = struct.unpack("<I", raw)[0]
        if not (0x10000 <= table < 0x7FFF0000):
            return []
        want_type = base + WIDGET_TYPE_RVA
        out = []
        for i in range(WIDGET_TABLE_MAX):
            r = _read_at(h, table + 4 * i, 4)
            if len(r) < 4:
                continue
            w = struct.unpack("<I", r)[0]
            if not (0x10000 <= w < 0x7FFF0000):
                continue
            t = _read_at(h, w + WIDGET_TYPE_AT, 4)
            if len(t) < 4 or struct.unpack("<I", t)[0] != want_type:
                continue
            np_ = _read_at(h, w + WIDGET_NAME_AT, 4)
            name = ""
            if len(np_) == 4:
                ptr = struct.unpack("<I", np_)[0]
                if ptr:
                    nm = _read_at(h, ptr, 72).split(NUL)[0]
                    if _IDENT.match(nm):
                        name = nm.decode()
            out.append((i, w, name))
        return out
    finally:
        k32.CloseHandle(h)


#: The class of the text label a widget displays, and where its string sits.
#: The same object the verdict is drawn on.
LABEL_VTABLE_RVA = 0xF93C44
LABEL_TEXT_AT = 0x3C

#: How far into a widget to look for the labels hanging off it. The map line
#: was seen at two different offsets in one lobby, so the whole head of the
#: object is searched.
WIDGET_LABEL_SPAN = 0x600
# The widget table's membership changes frame to frame, so a single sweep
# finds the map line only about half the time; retry a few times.
MAP_WIDGET_TRIES = 6
MAP_WIDGET_WAIT = 0.05
# The label itself, once found, does not move. Look it up once, read it
# directly, and sweep again only when it goes bad or the cache ages out.
MAP_WIDGET_REFRESH = 8.0
_map_label = {}


def read_map_widget(pid=None, base=None, fresh=False, tries=None):
    """The lobby's map, read off the widget that displays it. "" if absent.

    Opens the process read-only. Searching memory for the map line cannot
    work, because every map line the game has shown is still in memory, and
    the status slot shows it only when the lobby happens to redraw it. The
    widgets in the table are the ones on screen, so a label hanging off one
    of them is being displayed now, and the walk is bounded.
    """
    if sys.platform != "win32":
        return ""
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return ""
    base = base or module_base(pid)
    if not base:
        return ""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return ""
    want = base + LABEL_VTABLE_RVA
    try:
        lbl, found_at = _map_label.get(pid, (0, 0.0))
        # A caller that must not be told about the last lobby asks for fresh:
        # the cached label keeps answering after its lobby is gone, and the
        # lobby browser is built on the same screen, so leaving one lobby for
        # another would otherwise name the old map until the cache aged out.
        if lbl and not fresh:
            named = _map_on_label(h, lbl, want)
            # Trust the cache until it ages out; past that, sweep, since the
            # lobby may have been left and rejoined on another map.
            if named and time.time() - found_at < MAP_WIDGET_REFRESH:
                return named
        _map_label.pop(pid, None)
        # Retrying pays only when the line is there and the pass missed it.
        # A caller polling steadily wants tries=1: on screens that do not
        # draw the map every try fails, and six of them cost a second each
        # pass for nothing.
        for attempt in range(tries or MAP_WIDGET_TRIES):
            if attempt:
                time.sleep(MAP_WIDGET_WAIT)
            lbl = _map_label_on_widgets(h, top_widgets(pid, base), want)
            if lbl:
                _map_label[pid] = (lbl, time.time())
                return _map_on_label(h, lbl, want)
    finally:
        k32.CloseHandle(h)
    return ""


def forget_map_widget(pid=None):
    """Drop the cached label, so the next read sweeps for it again."""
    if pid is None:
        _map_label.clear()
    else:
        _map_label.pop(pid, None)


def _map_on_label(h, lbl, want):
    """This label's text, as a map name; "" if it is not one any more."""
    vt = _read_at(h, lbl, 4)
    if len(vt) < 4 or struct.unpack("<I", vt)[0] != want:
        return ""
    tp = _read_at(h, lbl + LABEL_TEXT_AT, 4)
    if len(tp) < 4:
        return ""
    return map_from_status(_utf16_at(h, struct.unpack("<I", tp)[0], 70))


def _map_label_on_widgets(h, ws, want):
    """The first label holding a map line, among these widgets' labels."""
    for _i, obj, _name in ws:
        # One read for the whole head of the widget rather than one per
        # offset.
        blk = _read_at(h, obj, WIDGET_LABEL_SPAN)
        for k in range(0, len(blk) - 3, 4):
            v = struct.unpack_from("<I", blk, k)[0]
            if not v or not (0x10000 <= v < 0x7FFF0000):
                continue
            raw = _read_at(h, v, 4)
            if len(raw) < 4 or struct.unpack("<I", raw)[0] != want:
                continue
            if _map_on_label(h, v, want):
                return v
    return 0


def in_battle(pid, base=None):
    """True when the battle's own UI is on screen. None when unreadable.

    The mirror of in_lobby: True is a sighting, False is "looked and did not
    see it", None is "could not look". A failed read must never move the
    state.
    """
    return _screen_has(pid, base, BATTLE_SCREEN_WIDGETS)


def in_lobby(pid, base=None):
    """Is the multiplayer lobby on screen? True/False, or None if unreadable.

    None is not False: a game that cannot be read has not said it left the
    lobby, and treating silence as "battle" is how a phantom start is made.
    """
    return _screen_has(pid, base, (LOBBY_SCREEN_WIDGET,))


def _screen_has(pid, base, names):
    """True if any of `names` is a widget on screen. None if unreadable."""
    ws = top_widgets(pid, base)
    if not ws:
        return None
    want = set(names)
    return any(n in want for _i, _a, n in ws)


def read_screen(pid, only=None, want=()):
    """What the UI is showing, structurally. (named, found) or (None, None).

    named   [(address, name, text)] for every widget with a real name
    found   {text: how many widgets show it} for text containing a `want` word

    Opens the process read-only and sweeps its regions for the widget type
    pointer. Both answers come out of one pass because reading the regions
    is the entire cost. `want` exists because the outcome is not on a named
    widget; it is matched as a substring and returned in full, so "Close
    Defeat" arrives as itself. `only` restricts the pass to regions whose
    base is in it, so a caller can hand over the regions that appeared since
    the battle began. (None, None) means the module base could not be read.
    """
    if sys.platform != "win32" or not pid:
        return None, None
    base = module_base(pid)
    if not base:
        return None, None
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None, None
    pat = struct.pack("<I", base + WIDGET_TYPE_RVA)
    found = {}
    named, seen = [], set()
    # Strings are shared: thousands of widgets resolve to a few hundred
    # distinct name pointers, so caching by pointer is most of the difference
    # between a nine-second pass and a one-second one.
    names, texts = {}, {}
    # Where the two pointers sit relative to the type field, so both can be
    # taken out of the buffer already in hand.
    NAME_FWD = WIDGET_NAME_AT - WIDGET_TYPE_AT
    TEXT_FWD = WIDGET_TEXT_AT - WIDGET_TYPE_AT
    try:
        for b0, size in _regions(h, max_region=256 << 20):
            if only is not None and b0 not in only:
                continue
            d = _read(h, b0, size)
            if not d:
                continue
            i = d.find(pat)
            while i >= 0:
                at = b0 + i
                obj = at - WIDGET_TYPE_AT
                # Both pointers have to be inside this region's bytes, which
                # lets the whole widget be read without touching the process
                # again. An object does not straddle a region in practice.
                if (at % 4 == 0 and obj not in seen
                        and i + TEXT_FWD + 4 <= len(d)):
                    seen.add(obj)
                    tp = struct.unpack_from("<I", d, i + TEXT_FWD)[0]
                    np = struct.unpack_from("<I", d, i + NAME_FWD)[0]
                    if tp not in texts:
                        texts[tp] = _utf16_at(h, tp)
                    text = texts[tp]
                    if text and any(word in text for word in want):
                        found[text] = found.get(text, 0) + 1
                    if np:
                        if np not in names:
                            raw = _read_at(h, np, 72)
                            nm = raw.split(NUL)[0]
                            names[np] = (nm.decode()
                                         if _IDENT.match(nm) else None)
                        if names[np]:
                            named.append((obj, names[np], text))
                i = d.find(pat, i + 1)
    finally:
        k32.CloseHandle(h)
    return named, found


def read_results_screen(pid, only=None):
    """Is the end-of-battle screen up, and what does it call the result?

    (True/False, {outcome text: count}, named widgets), or (None, None, [])
    when the game could not be read, which is the signal to fall back rather
    than to conclude no. Kept separate from read_screen so the widget names
    live in one place.
    """
    named, found = read_screen(pid, only=only, want=RESULT_WORDS)
    if named is None:
        return None, None, []
    return any(n in RESULTS_WIDGETS for _, n, _ in named), found, named


def _read_at(h, addr, size):
    """Read `size` bytes at addr through handle h (ReadProcessMemory), without
    needing the region list. b"" on failure."""
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                                 ctypes.byref(got)):
        return b""
    return buf.raw[:got.value]

# Which map the lobby is on, by thumbnail loads.
#
# Every preset key is in memory whatever is selected, because the map picker
# holds the whole catalogue. What singles out the chosen one is how many times
# the game has resolved its resource path: the lobby loads the selected map's
# thumbnail again to show it large, so that map ends up with about four
# references where the others have two.
#
#   data/BattleTerrain/Presets/<key>/screenshot_small.tga     x3 for the choice
#   data/BattleTerrain/Presets/<key>/preview_map.tga          x1 for everyone
#
# Forward slashes and a data/ prefix mark these as runtime loads; the pack
# catalogue is UTF-16 with backslashes and lists every map.
MAP_ANCHOR = b"/Presets/"
MAP_RE = re.compile(rb"data/[Bb]attle[Tt]errain/[Pp]resets/([a-z0-9_]+)/")

#: The map named in the lobby's own Info line, e.g.
#: "Italy 6-B - LOC: 70 minutes". The signal to trust when it answers: the
#: thumbnail count moves only when the map picker is opened, which a guest
#: never does, and the browser record exists only for lobbies reached through
#: the browser list. The Info line is drawn by the lobby on screen, in this
#: client's own language, so it cannot be stale and it resolves through
#: mapnames even when the host runs another language build.

#: The separator the Info line always uses, in UTF-16. Searched for directly
#: and walked back from, instead of a regex with a variable-length prefix,
#: which cost seconds per scan.
INFO_SEP = " - LOC:".encode("utf-16-le")

#: Longest map name worth walking back through, in UTF-16 characters.
INFO_MAX_NAME = 48


def info_names(d):
    """Every map name that appears before an Info-line separator in d.

    Walks backwards two bytes at a time, keeping the parity of the UTF-16
    string, and stops at the first byte that cannot be part of one: a NUL
    ends the string, a newline ends the line.
    """
    out, at = [], d.find(INFO_SEP)
    while at != -1:
        start = at
        while start >= 2 and at - start < INFO_MAX_NAME * 2:
            lo, hi = d[start - 2], d[start - 1]
            if hi != 0 or lo in (0, 10, 13):
                break
            start -= 2
        if start < at:
            out.append(d[start:at].decode("utf-16-le", "replace").strip())
        at = d.find(INFO_SEP, at + len(INFO_SEP))
    return out


#: What the Info line named on the last reading, as a map key, or None. Kept
#: beside the reader rather than added to the roster tuple, which every
#: caller unpacks positionally.
_info_map = None


def info_map():
    """The lobby's own Info line resolved to a map key, or None.

    The best of the three map signals when it answers; see INFO_SEP. Callers
    should prefer this and fall back to the browser record, then preset
    counts. Unanimous or silent: every readable copy of the line must name
    the same map, because a leftover from an earlier lobby cannot be told
    from the live one by position, and may outnumber it, but it does
    disagree with it.
    """
    return _info_map


def _note_info(votes, d):
    """Add every Info-line map name in d to votes. True if any resolved.

    Collects rather than trusting the first hit. The engine packs these
    strings together, so a match can begin inside the preceding string
    ("inutesItaly 6-B"); resolving through the name table throws those away.
    And a previous lobby's Info line can still be lying around, sometimes
    outnumbering the live one, which is why info_map requires unanimity.
    """
    hit = False
    for shown in info_names(d):
        key = mapnames.to_key(shown)
        if key:
            votes[key] += 1
            hit = True
    return hit


# How far ahead the thumbnail winner must be. Two is every map's baseline, so
# a lead over the runner-up is the whole signal.
MAP_MIN_HITS = 3

# The lobby BROWSER's listing: a tagged, length-prefixed string per open game,
#
#   00 00 00 00 0b <len+1> <key> 00 00 00 00 00 00
#
# Every open lobby on the list contributes one, so this names several maps at
# once and cannot say which is yours. It serves as a warning: a populated
# listing means "you have been browsing", which is exactly when the thumbnail
# count is stale, so a thumbnail lead that is not among the listed maps is
# refused.
LOBBY_LIST_RE = re.compile(
    rb"\x00{4}\x0b[\x05-\x40](ntw3_[a-z0-9_]{4,40})\x00{4}")

# The lobby browser's records. Every open game on the list writes a run
# holding the host's name, the map as the lobby displays it, the settings,
# and the host's SteamID64:
#
#   Badeblume ... 1813 - Dennewitz ... 36PTS TOW ... 76561198245643380
#
# The map is named because somebody is hosting on it, so it cannot be a
# leftover of our own. Which record is OUR lobby is settled by the roster:
# the record whose host SteamID is sitting with us is ours. Only fires as a
# guest; hosting, our lobby is not on our own browser list, and that is when
# the thumbnail count is right.
LOBBY_SID_RE = re.compile(rb"(7656119\d{10})")

# The map name, read by its length byte rather than by a character class,
# which truncated "1809 - A Coruña" at the first non-ASCII byte.
LOBBY_TAG_RE = re.compile(rb"\x0b([\x08-\x40])(?=1[6-9]\d{2} - )")
# How far back from the SteamID the map name sits: about 90 bytes in the
# captures, 300 is slack. The LAST match before the id is taken so a wider
# window cannot drag in the previous record's map.
LOBBY_LOOKBACK = 300
# A real listing repeats each map dozens of times; stray single hits are not
# one.
LOBBY_LIST_HITS = 10
LOBBY_LIST_MIN = 5

# Pacing for the full scan: work a little, yield a little. The scan is
# already fine-grained (thousands of short reads, none longer than a few
# frames); what pacing buys is lower sustained memory-bandwidth load on the
# machine the game is also using. The wall-clock cost does not matter because
# full scans only happen in a lobby.
SCAN_SLICE = 0.04     # work for this long...
SCAN_PAUSE = 0.02     # ...then let everything else run for this long


def read_roster(pid=None, include_ai=INCLUDE_AI_DEFAULT):
    """[(name, steamid64)] for everyone in the lobby. Empty if not in one."""
    return read_roster_state(pid, include_ai)[0]


def count_lobby_widget(pid, bases, seen):
    """Is the lobby back? (count of LOBBY_WIDGET, updated seen-set), cheaply.

    Opens the process read-only and counts LOBBY_WIDGET in two places only:
    regions that held it last time the lobby was open, and regions committed
    since. Together those cover it, at about 10 MB against well over a
    gigabyte for a full scan, so the end of a battle is noticed every half
    second instead of every thirty.
    """
    if sys.platform != "win32" or not pid:
        return 0, seen
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return 0, seen
    found = 0
    now = set()
    try:
        for base, size in _regions(h):
            now.add((base, size))
            if base not in bases and (base, size) in seen:
                continue
            d = _read(h, base, size)
            if d:
                found += d.count(LOBBY_WIDGET)
    finally:
        k32.CloseHandle(h)
    return found, now


def region_set(pid):
    """Which regions exist right now, without reading any of them.

    Opens the process read-only and enumerates regions with VirtualQueryEx,
    which is cheap. Used to seed the battle scan's seen-set per battle, so
    "new" means new since this battle began: a seen-set kept for the life of
    the client stopped working on long battles because the game reuses
    addresses.
    """
    if sys.platform != "win32" or not pid:
        return set()
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return set()
    try:
        return {(base, size) for base, size in _regions(h)}
    finally:
        k32.CloseHandle(h)


#: The verdict as the game draws it, in UTF-16. A third way of asking for
#: the result that needs no vtable, RVA or widget name: the same
#: needle-in-fresh-regions scan that detects the save screen, looking for
#: the words themselves. Costs one more find() over bytes already read.
RESULT_NEEDLES = tuple(w.encode("utf-16-le") for w in RESULT_WORDS)


def _phrase_at(d, i, limit=24):
    """The verdict the hit at `i` belongs to, or "".

    Grown outwards over letters and spaces rather than to the nearest NUL,
    because a drawn string in a live heap is not reliably NUL-delimited and
    walking to a terminator returned garbage glued to the front. Then
    checked: one to three Title-Case words, the last of which is a result
    word. That is the shape of every verdict the game owns, and it rejects
    strings that merely contain one ("Microsoft DirectDraw", "Victory
    Conditions").
    """
    def ch(k):
        if k < 0 or k + 1 >= len(d):
            return ""
        c = d[k:k + 2].decode("utf-16-le", "ignore")
        if len(c) != 1 or not c.isascii():
            # ASCII only: isalpha() is true for CJK, which let an overwritten
            # free-list pointer read as part of the phrase.
            return ""
        return c if c.isalpha() or c == " " else ""

    lo = i
    while lo >= 2 and (i - lo) // 2 < limit and ch(lo - 2):
        lo -= 2
    hi = i
    while (hi - i) // 2 < limit and ch(hi):
        hi += 2
    # Whatever the walk stopped on says what the phrase is part of. An ASCII
    # letter, digit or underscore there means this sits inside a longer
    # identifier such as "..._Advice_Victory_Conditions_Thread". Non-ASCII
    # is allowed: that is a freed block whose front was overwritten.
    def edge(k):
        if k < 0 or k + 1 >= len(d):
            return ""
        c = d[k:k + 2].decode("utf-16-le", "ignore")
        return c if len(c) == 1 else ""

    for c in (edge(lo - 2), edge(hi)):
        if c.isascii() and (c.isalnum() or c == "_"):
            return ""
    t = d[lo:hi].decode("utf-16-le", "ignore").strip()
    words = t.split()
    if not 1 <= len(words) <= 3 or words[-1] not in RESULT_WORDS:
        return ""
    # Title Case, every word. A phrase that starts mid-word is a copy whose
    # front has been overwritten, and one with a joining word is prose.
    if not all(w[:1].isupper() for w in words):
        return ""
    return t


#: The lobby draws its chosen battlefield as one line: the map, then how long
#: the battle may run, e.g. "1799 - Stockach - LOC: 70 minutes". Rare enough
#: to be an identity: the separator occurs a handful of times in the whole
#: process, where a map's display name or preset key occurs hundreds of
#: times because the catalogue holds every map.
MAP_LINE = " - LOC: "


def map_from_status(text):
    """The lobby's chosen map, from a status line, or "".

    Returns the name as the lobby displays it ("1799 - Stockach"), which the
    server resolves to a preset key. A forward-pointer answer that replaces
    the thumbnail count's threshold.
    """
    if not text or MAP_LINE not in text:
        return ""
    name = text.split(MAP_LINE)[0].strip()
    # Guard against a stray string that happens to carry the separator: the
    # lobby's own is short and has no control characters in it.
    if not name or len(name) > 60 or not name.isprintable():
        return ""
    return name


#: The map line as bytes, for finding it in memory rather than waiting for it
#: to pass through the status slot.
MAP_LINE_NEEDLE = MAP_LINE.encode("utf-16-le")

#: Regions that held the map line last time, so the next read is a few
#: milliseconds instead of a sweep. Memory only.
_map_line_bases = set()


def read_map_line(pid=None, hints=None):
    """The lobby's chosen map, read from memory now. (name, bases).

    Opens the process read-only. `name` is the map as the lobby displays it,
    "" when not found; `bases` are the regions that held the line, to be
    handed back as `hints` next time. Reading the string itself rather than
    latching what the status slot showed means what is there now is what the
    lobby shows now, and a stale map cannot be mistaken for a current one.
    Affordable every pass because the needle is nearly unique.
    """
    if sys.platform != "win32":
        return "", set()
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return "", set()
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return "", set()
    look = hints if hints is not None else _map_line_bases
    found, bases = "", set()
    try:
        for base, size in _regions(h, max_region=256 << 20):
            # Hinted regions only, when we have hints. A miss falls back to
            # the full sweep by being called with hints=set().
            if look and base not in look:
                continue
            d = _read(h, base, size)
            if not d:
                continue
            i = d.find(MAP_LINE_NEEDLE)
            while i >= 0:
                if i % 2 == 0:
                    name = _line_before(d, i)
                    if name:
                        # The LONGEST candidate, not the first. Some copies
                        # sit against the front of their allocation, where
                        # the walk backwards runs out of buffer and returns
                        # "99 - Trebbia" for "1799 - Trebbia", which resolves
                        # to no map at all.
                        if len(name) > len(found):
                            found = name
                        bases.add(base)
                i = d.find(MAP_LINE_NEEDLE, i + 1)
    finally:
        k32.CloseHandle(h)
    return found, bases


def _line_before(d, i, limit=60):
    """The map name that precedes the separator at `i`, or "".

    Grown backwards over the characters a map name is made of, not to the
    nearest NUL, since a drawn string in a live heap is not reliably
    terminated on its left. Letters rather than ASCII letters, because
    "1809 - A Coruña" has an n-tilde.
    """
    def ch(k):
        if k < 0 or k + 1 >= len(d):
            return ""
        c = d[k:k + 2].decode("utf-16-le", "ignore")
        if len(c) != 1:
            return ""
        # ASCII, plus the Latin letters the names use. Not "any
        # alphanumeric": CJK counts as alphanumeric in Python and let
        # unrelated heap in front of the string join the name.
        if c.isascii():
            return c if (c.isalnum() or c in " -()'.") else ""
        return c if (ord(c) < 0x250 and c.isalpha()) else ""

    lo, n = i, 0
    while lo >= 2 and n < limit and ch(lo - 2):
        lo -= 2
        n += 1
    t = d[lo:i].decode("utf-16-le", "ignore").strip()
    # A map name has a letter in it. "70" alone is the minutes, not a map.
    if not t or len(t) > 60 or not any(c.isalpha() for c in t):
        return ""
    return t


#: The lobby's seat array, found by its OWNER rather than by hunting the
#: array itself. Almost nothing points at the array, but one object holds it
#: and that object has a recognisable type:
#:
#:     [obj]        == base + ROSTER_OWNER_VTABLE_RVA
#:     [obj + 4]    == ROSTER_OWNER_TAG
#:     [obj - 0x1C] -> the array's allocation; records start 8 bytes in
#:
#: The vtable and tag alone match hundreds of objects. What makes it exact is
#: checking that the pointed-at memory really is a seat array: every
#: SteamID64 has 0x01100001 as its high dword, so one read says whether the
#: first record is a player. Exactly one candidate survives per capture.
ROSTER_OWNER_VTABLE_RVA = 0x10A2F50
ROSTER_OWNER_TAG = 0x36
ROSTER_ARRAY_AT = -0x1C          # from the owner to the array pointer
ROSTER_FIRST_RECORD = 8          # the allocation header in front of record 0
ROSTER_STRIDE = 0x60
SID_HIGH_DWORD = 0x01100001

#: Regions that held the owner last time, so the next read is milliseconds.
_roster_owner_bases = set()
#: The owner object itself, from the last successful find_roster_array. The
#: lobby's configured size hangs off it (see roster.CAPACITY_AT).
_roster_owner = [0]


def roster_owner():
    """Address of the seat array's owner from the last find, or 0."""
    return _roster_owner[0]


def find_roster_array(pid=None, hints=None):
    """(array address, regions) for the lobby's seat array, or (0, set()).

    Opens the process read-only and searches for the owner object described
    at ROSTER_OWNER_VTABLE_RVA. The array's allocation is returned; the first
    seat record is ROSTER_FIRST_RECORD bytes into it and they run at
    ROSTER_STRIDE. `hints` restricts the search to the regions that held the
    owner last time; pass hints=set() to sweep everything.
    """
    if sys.platform != "win32":
        return 0, set()
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return 0, set()
    base = module_base(pid)
    if not base:
        return 0, set()
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return 0, set()
    look = hints if hints is not None else _roster_owner_bases
    pat = struct.pack("<I", base + ROSTER_OWNER_VTABLE_RVA)
    found, bases = 0, set()
    try:
        for b0, size in _regions(h, max_region=256 << 20):
            if look and b0 not in look:
                continue
            d = _read(h, b0, size)
            if not d:
                continue
            i = d.find(pat)
            while i >= 0:
                a = b0 + i
                if a % 4 == 0 and _u32(h, a + 4) == ROSTER_OWNER_TAG:
                    arr = _u32(h, a + ROSTER_ARRAY_AT)
                    if arr and 0x10000 <= arr < 0x7FFF0000:
                        # Is the first record a player? Every SteamID64 has
                        # the same high dword, so one read separates the real
                        # owner from objects that merely share type and tag.
                        if _u32(h, arr + ROSTER_FIRST_RECORD + 4) == SID_HIGH_DWORD:
                            if not found:
                                found = arr
                                _roster_owner[0] = a
                            bases.add(b0)
                i = d.find(pat, i + 1)
    finally:
        k32.CloseHandle(h)
    return found, bases


def _u32(h, addr):
    """The little-endian dword at addr through handle h, or 0."""
    raw = _read_at(h, addr, 4)
    return struct.unpack("<I", raw)[0] if len(raw) == 4 else 0


def is_verdict(t):
    """Is this string the game's own name for a result?

    One to three Title-Case words whose last is Victory, Defeat or Draw.
    Memory is full of strings that merely contain one of those words, and a
    substring test reports them as results.
    """
    w = (t or "").split()
    return (1 <= len(w) <= 3 and w[-1] in RESULT_WORDS
            and all(x[:1].isupper() for x in w))


def verdicts_in(d):
    """{phrase: copies} for every result string drawn in a block of memory."""
    out = {}
    for needle in RESULT_NEEDLES:
        i = d.find(needle)
        while i >= 0:
            # Even offsets only: a region starts on a page boundary, so an
            # odd hit is two other characters' bytes read out of step.
            if i % 2 == 0:
                t = _phrase_at(d, i)
                if t:
                    out[t] = out.get(t, 0) + 1
            i = d.find(needle, i + 1)
    return out


def count_verdicts(pid, max_region=256 << 20):
    """{phrase: copies} for every result string in the game, or None.

    Opens the process read-only and does one full sweep, so it is called
    twice in a battle, not every pass; what matters is the DIFFERENCE between
    the two calls. Every phrase the game can call a result is resident from
    startup in the localisation table, so presence says nothing; the verdict
    is the count that rose when the screen was built, and in the captures
    nothing else moved. This needs no vtable, RVA or widget names, so a
    patch that moves them cannot break it. None means the game could not be
    read.
    """
    if sys.platform != "win32" or not pid:
        return None
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    out = {}
    try:
        for base, size in _regions(h, max_region=max_region):
            d = _read(h, base, size)
            if not d:
                continue
            for t, n in verdicts_in(d).items():
                out[t] = out.get(t, 0) + n
    finally:
        k32.CloseHandle(h)
    return out


def verdicts_risen(base, now):
    """What is on screen that was not before: {phrase: how many more}.

    Both arguments come from count_verdicts. A phrase absent from the
    baseline counts as zero rather than being skipped.
    """
    if not base or not now:
        return {}
    return {t: n - base.get(t, 0) for t, n in now.items()
            if n > base.get(t, 0)}


#: What scan_battle_signals looks for, and the name each answer goes by.
BATTLE_NEEDLES = {
    "save": SAVE_WIDGET,
    "host_left": HOST_LEFT,
    "dropped": SELF_DROPPED,
}


def scan_battle_signals(pid, seen, tally=None, fresh=None):
    """Count all three battle signals cheaply during a battle.

    Returns (counts, seen, tally, fresh). Opens the process read-only. The
    full scan does not run during a battle, so the host-left and dropped
    dialogs can only be noticed here.

    Three things separate it from a plain new-regions delta. A region that
    has held a needle is re-read every pass (the tally), because a dialog can
    appear in a region that already existed. A region that has only just
    appeared is re-read for FRESH_SECONDS (the fresh map), because memory is
    allocated before it is written. And counts are held per region and
    summed, so the total is what is on screen now however it arrived, and
    re-reading a region cannot double-count it.
    """
    counts = {k: 0 for k in BATTLE_NEEDLES}
    if sys.platform != "win32" or not pid:
        return counts, seen, dict(tally or {}), dict(fresh or {})
    tally = dict(tally or {})
    fresh = dict(fresh or {})
    at = time.monotonic()
    # Which of the recently appeared regions get another look this pass.
    # Newest first, and capped so one pass cannot stall the loop.
    redo, spend = set(), 0
    for key in sorted(fresh, key=lambda k: fresh[k], reverse=True):
        if at - fresh[key] >= FRESH_SECONDS or key[1] > FRESH_MAX_BYTES:
            continue
        if spend + key[1] > FRESH_BUDGET:
            break
        redo.add(key)
        spend += key[1]

    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return counts, seen, tally, fresh
    now = set()
    try:
        for base, size in _regions(h):
            key = (base, size)
            now.add(key)
            if key not in seen:
                # Brand new. Always read, and start its clock so the next
                # few passes look again.
                fresh[key] = at
            elif key not in tally and key not in redo:
                # Old, holding nothing, and not recent enough for another
                # look. Nearly all of a battle's regions end up here.
                continue
            d = _read(h, base, size)
            if not d:
                continue
            got = {k: d.count(n) for k, n in BATTLE_NEEDLES.items()}
            if any(got.values()):
                tally[key] = got
            else:
                tally.pop(key, None)
    finally:
        k32.CloseHandle(h)
    # A region that has gone takes its count with it.
    tally = {k: v for k, v in tally.items() if k in now}
    fresh = {k: v for k, v in fresh.items()
             if k in now and at - v < FRESH_SECONDS}
    for got in tally.values():
        for k, n in got.items():
            counts[k] += n
    return counts, now, tally, fresh


def save_button_showing(saves, baseline):
    """Is the Save Replay button on screen, given what the count has been?

    baseline is the count while the battle was running. None means it was
    never sampled, in which case this says no.
    """
    if baseline is None:
        return False
    return saves >= baseline + SAVE_JUMP


def read_roster_state(pid=None, include_ai=INCLUDE_AI_DEFAULT):
    """The full lobby scan. Opens the process read-only and walks every
    readable region once.

    Returns ([(name, steamid64)], state, save_count, bases, map_key, hosts,
    signals), seven values on every path, so the game closing (a normal
    event) does not raise in callers that unpack it.

    The state, the save count, the map signals and the lobby-browser records
    all come out of the same pass: the scan already reads every region, so
    counting a few more strings costs only the comparison. save_count is raw
    on purpose; only a caller watching it over time can say what it means
    (see SAVE_WIDGET). hosts is {host SteamID64: map as displayed} from the
    browser records, handed back unmatched because the caller has the better
    roster from live_seats to match it against.
    """
    if sys.platform != "win32":
        return [], STATE_UNKNOWN, 0, set(), None, {}, {}
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return [], STATE_UNKNOWN, 0, set(), None, {}, {}
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return [], STATE_UNKNOWN, 0, set(), None, {}, {}

    # Count every (id, name) seen and let frequency decide. The name half of
    # the pattern runs back over whatever printable bytes precede the real
    # string, so individual matches can be corrupt; the genuine name is the
    # one that recurs, since the game holds it in several places.
    from collections import Counter
    votes = {}
    cpus = set()
    widget = 0
    widget_bases = set()
    saves = 0
    maps = Counter()
    listed = Counter()
    hosts = {}
    record_bases = set()
    results_bases = set()
    info = Counter()
    markers = [s.encode("utf-16-le") for s in LOBBY_MARKERS]
    slice_start = time.monotonic()
    try:
        for base, size in _regions(h):
            # Yield between slices so the game is never blocked for long.
            # Checked per region: regions are small enough already.
            if time.monotonic() - slice_start >= SCAN_SLICE:
                time.sleep(SCAN_PAUSE)
                slice_start = time.monotonic()
            d = _read(h, base, size)
            if not d:
                continue
            n = d.count(LOBBY_WIDGET)
            if n:
                widget += n
                widget_bases.add(base)
            saves += d.count(SAVE_WIDGET)
            # Guarded by a plain substring test: the regex is too costly to
            # run over every region. "/Presets/" is rare because the pack
            # catalogue spells it with backslashes and in UTF-16.
            if MAP_ANCHOR in d:
                for m in MAP_RE.finditer(d):
                    maps[m.group(1).decode("ascii", "replace")] += 1
            # The end-of-battle table, noticed on a walk that is happening
            # anyway, for one substring test per region.
            if RESULTS_ANCHOR in d:
                results_bases.add((base, size))
            if INFO_SEP in d and _note_info(info, d):
                record_bases.add((base, size))
            if b"ntw3_" in d:
                hit = False
                for m in LOBBY_LIST_RE.finditer(d):
                    listed[m.group(1).decode("ascii", "replace")] += 1
                    hit = True
                if hit:
                    record_bases.add((base, size))
            if b"7656119" in d:
                for m in LOBBY_SID_RE.finditer(d):
                    back = d[max(0, m.start() - LOBBY_LOOKBACK):m.start()]
                    shown = shown_names(back)
                    if shown:
                        hosts.setdefault(int(m.group(1)), shown[-1])
                        record_bases.add((base, size))
            # AI seats, in every region. Restricting this to regions with
            # lobby markers lost seats: the same lobby spreads its rows
            # across several regions, some carrying no marker.
            if include_ai and b"p\x00l\x00a\x00y\x00e\x00r\x00_\x00r\x00a\x00n\x00k\x00" in d:
                for nm in live_seats(d):
                    cpus.add(nm)
            # Cheap reject: no SteamID prefix anywhere, no human roster here.
            if b"1100001" not in d:
                continue
            for m in ROSTER_RE.finditer(d):
                try:
                    # UTF-8, not ASCII. Decoding is also the validity test:
                    # the bytes before a real name are memory, and memory
                    # rarely forms valid UTF-8.
                    name = m.group(1).decode("utf-8").strip()
                    sid = int(m.group(2), 16)
                except (UnicodeDecodeError, ValueError):
                    continue
                if sid <= STEAMID64_BASE or not name:
                    continue
                votes.setdefault(sid, Counter())[name] += 1
    finally:
        k32.CloseHandle(h)

    out = [(name, None) for name in sorted(cpus)]
    for sid, counter in votes.items():
        # Most common first; ties broken toward the shorter name, since extra
        # leading bytes only ever make a candidate longer than the truth.
        name = min(counter.most_common(),
                   key=lambda kv: (-kv[1], len(kv[0])))[0]
        out.append((clean_name(name), sid))

    # Which screen is up. The widget count is the fallback, because the
    # forward walk needs the module base and a build whose table has not
    # moved; when in_lobby can answer, it wins. The walk CONFIRMS a lobby and
    # does not deny one: its False also covers the main menu and the lobby
    # browser, and reading that as "battle" produced phantom battle starts.
    state = STATE_LOBBY if widget >= LOBBY_WIDGET_MIN else STATE_BATTLE
    if in_lobby(pid):
        state = STATE_LOBBY
    # hosts is handed back rather than resolved here. Matching it needs the
    # roster, and the roster this function returns is the weaker of the two
    # the client has; the caller has live_seats, which walks the seat array.
    global _map_bases, _results_bases, _info_map
    _map_bases = record_bases
    _results_bases = results_bases
    _info_map = next(iter(info)) if len(info) == 1 else None
    # Empty, deliberately. The dialogs are read by scan_battle_signals during
    # the battle, which is the only time they can be on screen.
    signals = {}
    return out, state, saves, widget_bases, pick_map(maps, listed), hosts, signals


#: The end-of-battle results table's column header, in UTF-16. Chosen as the
#: anchor because it appears only while that screen is up.
RESULTS_ANCHOR = "Enemy Killed".encode("utf-16-le")

#: Regions carrying that header on the last scan, as {(base, size)}. Empty
#: whenever the results screen is not showing.
_results_bases = set()


def results_regions():
    """Where the results table was last seen, or an empty set.

    The screen is up for seconds and the table's cells are filled a moment
    after its rows appear, so a caller that finds this non-empty should read
    it immediately.
    """
    return set(_results_bases)


#: Where the lobby records were last seen: {(base, size)}. One region of a
#: few MB out of hundreds readable, so re-reading just these is milliseconds
#: against seconds for a full pass. Memory only.
_map_bases = set()


def read_map(pid=None, hints=None):
    """(map_key, hosts) from the cached regions only. Milliseconds, not seconds.

    Opens the process read-only and re-reads only the regions that held
    lobby records last time, cheaply enough to run between full scans.
    Returns (None, {}) when the hints yield nothing; that is not an error, the
    caller should keep taking the full reading periodically.
    """
    if sys.platform != "win32":
        return None, {}
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return None, {}
    bases = hints if hints is not None else _map_bases
    if not bases:
        return None, {}

    from collections import Counter

    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                        False, pid)
    if not h:
        return None, {}
    listed = Counter()
    hosts = {}
    info = Counter()
    try:
        for base, size in bases:
            # Read the address directly rather than re-enumerating: if the
            # region has gone, ReadProcessMemory simply returns nothing.
            d = _read(h, base, size)
            if not d:
                continue
            if INFO_SEP in d:
                _note_info(info, d)
            if b"ntw3_" in d:
                for m in LOBBY_LIST_RE.finditer(d):
                    listed[m.group(1).decode("ascii", "replace")] += 1
            if b"7656119" in d:
                for m in LOBBY_SID_RE.finditer(d):
                    back = d[max(0, m.start() - LOBBY_LOOKBACK):m.start()]
                    shown = shown_names(back)
                    if shown:
                        hosts.setdefault(int(m.group(1)), shown[-1])
    finally:
        k32.CloseHandle(h)

    # Cleared rather than left alone when nothing was seen: leaving the last
    # answer standing would keep naming a lobby the player has already left.
    global _info_map
    _info_map = next(iter(info)) if len(info) == 1 else None

    # Only the browser-list signal is available here; the thumbnail count is
    # the hosting case, which a full pass covers.
    return pick_map(Counter(), listed), hosts


def shown_names(buf):
    """Every "<year> - <Map>" tagged string in buf, in order.

    Read by the length byte, so an accented letter is carried through.
    Decoded as UTF-8 with a Latin-1 fallback; the server folds accents when
    matching, so either spelling finds the map.
    """
    out = []
    for m in LOBBY_TAG_RE.finditer(buf):
        n = buf[m.start(1)] - 1
        raw = buf[m.end(1):m.end(1) + n]
        if len(raw) < n:
            continue
        try:
            out.append(raw.decode("utf-8").strip())
        except UnicodeDecodeError:
            out.append(raw.decode("latin-1").strip())
    return out


def pick_lobby_map(hosts, sids):
    """The map of the lobby we are in, from the browser records. None if unsure.

    hosts is {host SteamID64: map as displayed}, one entry per open game on
    the browser list. sids is who is sitting in the lobby with us. Our host
    is one of them, so the record they own names our map. Requires a single
    answer; picking between two would be a guess.
    """
    if not hosts or not sids:
        return None
    mine = {name for sid, name in hosts.items() if sid in sids}
    return mine.pop() if len(mine) == 1 else None


#: Lobby-record hits needed before that signal is believed. Hosting our own
#: game the browser list is all but empty, while guesting it runs to dozens,
#: so the threshold doubles as the test for which situation we are in.
LIST_MIN_HITS = 5

#: How far ahead the leader must be. A lobby we are sitting in is referenced
#: far more than one merely listed beside it; when the top of the list is a
#: photo finish, none of them is our lobby.
LIST_MIN_LEAD = 1.5


def pick_map(counts, listed=None):
    """The current map, from whichever signal applies. None when unsure.

    `counts` is thumbnail loads, which move when the map picker is opened
    (hosting) and otherwise keep naming whatever we last chose ourselves.
    `listed` is the lobby-browser records, nearly empty when hosting and rich
    when guesting.
    """
    # Thumbnails first, and the list only when they are silent. The list
    # used to win, on the theory that a populated list means we are a guest;
    # but cancelling a lobby does not clear the list, so a host who had just
    # cancelled another game was named the wrong map. There is no clean
    # threshold between the two cases. The ordering is safe because the
    # guest case is answered before this is reached: pick_lobby_map matches a
    # browser record against the people in our lobby.
    if counts:
        ranked = counts.most_common(2)
        key, n = ranked[0]
        if n >= MAP_MIN_HITS and not (len(ranked) > 1 and ranked[1][1] >= n):
            return key

    # Nothing from the picker: fall back to a browser list with a clear leader.
    if listed:
        ranked = listed.most_common(2)
        key, n = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0
        if n >= LIST_MIN_HITS and n >= runner * LIST_MIN_LEAD:
            return key
    return None


def clean_name(name):
    """Trim leading junk the greedy match may have swallowed.

    Steam names cannot start with most ASCII punctuation, so a leading run of
    it is almost certainly memory that sat before the string. Clan tags in
    brackets are real and preserved. Only ASCII is trimmed: a leading
    non-ASCII character is part of the name ("¤sprucedoors").
    """
    while name and ord(name[0]) < 128 and not (
            name[0].isalnum() or name[0] in "[(_<{|"):
        name = name[1:]
    return name.strip()


def main():
    """Command-line entry: print the lobby roster once, or whenever it changes."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--no-ai", action="store_true",
                    help="hide AI seats (they can be stale: freed memory keeps "
                         "removed ones)")
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    pid, exe = find_game()
    if not pid:
        print("Napoleon is not running.")
        return 1
    print(f"reading {exe} (pid {pid})")

    if not args.watch:
        r = read_roster(pid, include_ai=not args.no_ai)
        if not r:
            print("no lobby roster found -- are you in a lobby?")
        show(r)
        return 0

    last = None
    print("watching. Ctrl-C to stop.")
    while True:
        r = sorted(read_roster(pid, include_ai=not args.no_ai), key=lambda t: (t[1] is None, t[0]))
        if r != last:
            last = r
            print(f"\n[{time.strftime('%H:%M:%S')}] {len(r)} seat(s)")
            show(r)
        time.sleep(args.interval)


def show(roster):
    """Print a roster to stdout, humans first."""
    humans = [r for r in roster if r[1]]
    ai = [r for r in roster if not r[1]]
    for name, sid in humans:
        print(f"  {name:<28} {sid}")
    for name, _ in ai:
        print(f"  {name:<28} AI")
    print(f"  -- {len(humans)} human, {len(ai)} AI")


if __name__ == "__main__":
    sys.exit(main() or 0)
