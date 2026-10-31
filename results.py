"""Read the end-of-battle results table out of the game's memory.

    python results.py --capture ../client/test/lobbydump_194223.bin
    python results.py --live                 (with the results screen up)

The replay cannot supply this: its BATTLE_RESULTS block is a pre-battle stub.
The results screen the game draws afterwards is the only source, and it exists
only while it is on screen.

Memory access is read-only, through lobby_memory (OpenProcess with
PROCESS_VM_READ). Nothing is written to the game, and nothing in this module
touches the network; the parsed rows go to the caller, or to a text report on
request.

Values in the table are rendered UTF-16 text, never integers. Each lives in a
UI string object:

    -0x0C  length     characters, matches the string exactly
    -0x08  capacity
    -0x04  tag        low half 0x014C; the high half varies
    +0x00  pointer to the UTF-16 text

Column headers, player names and army labels are the same kind of object, so
the whole table reads out of one enumeration.

Timing is the hard part: rows bind their names first and fill their cells
afterwards, and the gap widens with more players. Living in the client, which
already knows which regions to re-read, makes the read fast enough to win.
"""

import argparse
import os
import re
import struct
import sys
import time

#: Only the results screen draws this column header, so its presence is the
#: screen's fingerprint. "Victory" and "Defeat" sit in the localisation table
#: from boot and are no use.
ANCHOR = "Enemy Killed".encode("utf-16-le")

#: The low half of a rendered-string object's tag. The upper half varies
#: between screens, so matching all 32 bits finds nothing.
TAG_LOW = 0x014C

#: Column headers, in the order the game draws them. Used to recognise the
#: table rather than to locate it.
HEADERS = ["Players", "Deployed", "Losses", "Enemy Killed", "Skill Rating",
           "Achievements"]

#: An army label as drawn: "[1814] 8. France", "[1809] 9. España".
ARMY = re.compile(r"^\[1[6-9]\d{2}\]\s*\d{1,2}\.")

#: A rendered integer.
NUMBER = re.compile(r"^\d{1,6}$")

#: How far apart two objects can be and still belong to the same table. The
#: cells sat well inside this in every capture; the next UI cluster was
#: megabytes away.
CLUSTER = 0x20000


def tagged_strings(regions, reader):
    """Every rendered string object in the regions, as [(address, text)].

    `regions` is [(base, size)] and `reader(base, size) -> bytes`, so the same
    code serves a live process and a capture file.
    """
    # Address -> bytes, so a pointer into an already-read region resolves
    # without going back to the source.
    have = []
    for base, size in regions:
        d = reader(base, size)
        if d:
            have.append((base, len(d), d))
    have.sort()

    def at(va, n):
        for base, size, d in have:
            if base <= va < base + size:
                off = va - base
                return d[off:off + n] if off + n <= size else None
        return None

    tag = struct.pack("<H", TAG_LOW)
    out = []
    for base, size, d in have:
        pos = 0
        while True:
            i = d.find(tag, pos)
            if i < 0:
                break
            pos = i + 1
            if i < 12 or i + 8 > size or (i % 4):
                continue
            length, cap = struct.unpack_from("<II", d, i - 8)
            if not (1 <= length <= 80) or not (length <= cap <= 512):
                continue
            ptr = struct.unpack_from("<I", d, i + 4)[0]
            raw = at(ptr, length * 2)
            if raw is None or len(raw) < length * 2:
                continue
            try:
                s = raw.decode("utf-16-le")
            except UnicodeDecodeError:
                continue
            if len(s) != length or not s.isprintable():
                continue
            out.append((base + i, s))
    out.sort()
    return out


def table_from(strings):
    """Group the densest cluster of strings into rows.

    A row is a player name, an army label and four numbers, adjacent in
    address order but not on a fixed stride. Returns (rows, raw) where raw is
    every string in the cluster, in order: the grouping is a guess and the
    strings are not, so a report carrying both can be re-read if the guess
    is wrong.
    """
    if not strings:
        return [], []

    # Located by army labels, not by column headers. The headers also sit in
    # the localisation table, megabytes away with no data near them.
    # "[1814] 8. France" appears once per row and nowhere else on the screen,
    # so the tightest group of them is the table.
    marks = [a for a, s in strings if ARMY.match(s)]
    if not marks:
        return [], []
    groups, cur = [], [marks[0]]
    for prev, a in zip(marks, marks[1:]):
        if a - prev < CLUSTER:
            cur.append(a)
        else:
            groups.append(cur)
            cur = [a]
    groups.append(cur)

    # Most labels wins; on a tie the tighter group, since the army catalogue
    # is a few hundred labels spread thinly and a table is a handful packed
    # together.
    best = max(groups, key=lambda g: (len(g), -(g[-1] - g[0])))
    lo = best[0] - CLUSTER
    hi = best[-1] + CLUSTER
    near = [(a, s) for a, s in strings if lo <= a <= hi]

    # Each row is anchored on its army label, which is unmistakable and occurs
    # exactly once per row. The player name is the nearest non-noise string
    # before it, and the row's numbers follow.
    rows = []
    for i, (_a, s) in enumerate(near):
        if not ARMY.match(s):
            continue

        # A long label wraps onto a second object: "[1805] 9. Rossiya," then
        # "Österreich". Join it back.
        army = s
        if i + 1 < len(near) and s.rstrip().endswith(","):
            nxt = near[i + 1][1]
            if not NUMBER.match(nxt) and not is_noise(nxt):
                army = f"{s} {nxt}"

        player = None
        for j in range(i - 1, max(-1, i - 8), -1):
            cand = near[j][1]
            if NUMBER.match(cand) or is_noise(cand) or ARMY.match(cand):
                continue
            player = cand
            break

        values = []
        for j in range(i + 1, min(len(near), i + 60)):
            cand = near[j][1]
            if ARMY.match(cand):
                break
            if NUMBER.match(cand):
                values.append(cand)
            if len(values) == 4:
                break

        rows.append({"player": player or "?", "army": army,
                     "values": values})

    return dedupe(rows), near


#: The number the widget template carries before a battle fills it in. It
#: appears interleaved with the real values, so it is filtered out. A real
#: army could in principle field 5000, which is why the raw strings are kept
#: in the report underneath.
PLACEHOLDER = "5000"


def dedupe(rows):
    """One row per player, keeping the one with the most real numbers.

    The table is built more than once (an unfilled template and the drawn
    copy), so the same player appears several times.
    """
    best = {}
    for r in rows:
        vals = [v for v in r["values"] if v != PLACEHOLDER]
        if r["player"] in ("?", "borde", "border"):
            continue
        score = len(vals)
        cur = best.get(r["player"])
        if cur is None or score > cur[0]:
            best[r["player"]] = (score, {**r, "values": vals})
    return [v for _s, v in best.values()]


#: Widget ids, headers and scenery that share the table's neighbourhood. A
#: name is what is left once these are gone.
NOISE = {
    "border", "Loading", "icon", "avatar", "name", "Name", "score", "kills",
    "losses", "spacer", "men_tx", "stat1", "stat3", "Effect", "Effe", "panel",
    "All", "Team", "Chat", "Votes:", "Swap Sides", "Rematch", "Exit",
    "Lost", "Kills", "Experience", "Deployed", "Crew Lost", "Ships Sunk",
    "Ships Captured", "Casualties Inflicted", "Battle Results",
    "Unit Statistics", "Achievements", "Skill Rating", "Players",
}


def is_noise(s):
    """True for strings that cannot be a player name: widget ids, headers, asset paths, garbage."""
    if s in NOISE or s in HEADERS:
        return True
    # Widget ids and asset paths, never a Steam name.
    if "\\" in s or "/" in s or "_tx" in s or s.endswith("_parent"):
        return True
    if s.startswith(("chat_", "panel_", "men_", "button_", "frame_")):
        return True
    # Text that did not decode as text.
    printable = sum(c.isascii() and c.isprintable() for c in s)
    return printable < len(s) * 0.5


def render(rows, raw, meta):
    """The report, as text a person can check against the screen."""
    out = []
    out.append("=" * 72)
    out.append("BATTLE RESULTS, read from memory")
    for k, v in meta.items():
        out.append(f"  {k:<12} {v}")
    out.append("=" * 72)
    out.append("")

    if not rows:
        out.append("  No table found. Either the results screen was not up,")
        out.append("  or its cells had not been filled yet -- the rows bind")
        out.append("  their names before their numbers.")
    else:
        out.append(f"  {'player':<30} {'army':<28} "
                   f"{'depl':>6} {'loss':>6} {'kill':>6} {'rating':>7}")
        out.append("  " + "-" * 88)
        for r in rows:
            v = r["values"] + ["", "", "", ""]
            out.append(f"  {r['player'][:29]:<30} {(r['army'] or '')[:27]:<28} "
                       f"{v[0]:>6} {v[1]:>6} {v[2]:>6} {v[3]:>7}")

    out.append("")
    out.append("=" * 72)
    out.append("EVERY STRING IN THE TABLE'S NEIGHBOURHOOD, IN ORDER")
    out.append("  Kept because the grouping above is inferred and this is not.")
    out.append("=" * 72)
    for a, s in raw:
        out.append(f"  0x{a:08X}  {s}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def from_capture(path):
    """Regions and a reader for a memdump.py capture file (read only)."""
    regions, offsets = [], {}
    with open(path, "rb") as f:
        while True:
            hdr = f.read(16)
            if len(hdr) < 16:
                break
            base, size = struct.unpack("<QQ", hdr)
            offsets[base] = f.tell()
            f.seek(size, 1)
            regions.append((base, size))

    def reader(base, size):
        with open(path, "rb") as f:
            f.seek(offsets[base])
            return f.read(size)

    return regions, reader


def from_known_regions(pid, regions):
    """Read-only reader for memory regions the caller has already located.

    Used by the overlay, whose poll already walks every region and notices
    the results header; handing the addresses here turns a second pass over
    the whole process into a read of a few megabytes. Neighbouring regions
    are taken too, because the string pointers reach outside the anchor's
    region. Opens the game process with PROCESS_VM_READ only.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import lobby_memory as lm

    if not pid or not regions:
        return [], None
    h = lm.k32.OpenProcess(
        lm.PROCESS_QUERY_INFORMATION | lm.PROCESS_VM_READ, False, pid)
    if not h:
        return [], None

    want = set(regions)
    cache, keep = {}, []
    try:
        for base, size in lm._regions(h):
            near = any(abs(base - b) < 0x400000 for b, _s in want)
            if (base, size) in want or near:
                d = lm._read(h, base, size)
                if d:
                    cache[base] = d
                    keep.append((base, size))
    finally:
        lm.k32.CloseHandle(h)
    return keep, lambda base, size: cache.get(base, b"")


def read_now(pid, regions, meta=None):
    """(text, rows) for the table in those regions, or (None, []) if absent."""
    got, reader = from_known_regions(pid, regions)
    if not got or reader is None:
        return None, []
    strings = tagged_strings(got, reader)
    rows, raw = table_from(strings)
    if not raw:
        return None, []
    info = {"source": "live", "regions": len(got), "strings": len(strings),
            "rows": len(rows), "read at": time.strftime("%Y-%m-%d %H:%M:%S")}
    info.update(meta or {})
    return render(rows, raw, info), rows


def from_process(pid=None):
    """Regions and a read-only reader for the running game, narrowed to the results screen.

    Two passes: a cheap substring search for ANCHOR over every region, then
    only the regions that carry it (plus their neighbours) are handed to the
    parser. Opens the game process with PROCESS_VM_READ only.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import lobby_memory as lm

    if pid is None:
        got = lm.find_game()
        pid = got[0] if got else None
    if not pid:
        return [], None

    h = lm.k32.OpenProcess(
        lm.PROCESS_QUERY_INFORMATION | lm.PROCESS_VM_READ, False, pid)
    if not h:
        return [], None

    keep, cache = [], {}
    try:
        for base, size in lm._regions(h):
            d = lm._read(h, base, size)
            if d and ANCHOR in d:
                keep.append((base, size))
                cache[base] = d
        # The pointers reach outside the anchor's own region, so take the
        # neighbours too.
        if keep:
            near = []
            for base, size in lm._regions(h):
                if any(abs(base - b) < 0x400000 for b, _s in keep):
                    if base not in cache:
                        d = lm._read(h, base, size)
                        if d:
                            cache[base] = d
                            near.append((base, size))
            keep.extend(near)
    finally:
        lm.k32.CloseHandle(h)

    return keep, lambda base, size: cache.get(base, b"")


def main():
    """Command-line entry point: read a capture or the live game and print or write the report."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", help="read a memdump.py .bin instead")
    ap.add_argument("--live", action="store_true", help="read the game now")
    ap.add_argument("--out", help="write the report here")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    t0 = time.time()
    if args.capture:
        regions, reader = from_capture(args.capture)
        src = os.path.basename(args.capture)
    else:
        regions, reader = from_process()
        src = "live process"
    if not regions or reader is None:
        print("nothing to read: the game is not running, or the results "
              "screen is not up")
        return 1

    strings = tagged_strings(regions, reader)
    rows, raw = table_from(strings)
    meta = {
        "source": src,
        "regions": len(regions),
        "strings": len(strings),
        "rows": len(rows),
        "seconds": f"{time.time() - t0:.1f}",
        "read at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    text = render(rows, raw, meta)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out}  ({len(rows)} rows)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
