"""Bound the engine's 100-slot unit buffer in Napoleon.exe (the "unit buffer" crash).

    python fixbuffer.py            report what state the exe is in, and the dry-run diff
    python fixbuffer.py --apply    back up, patch, verify
    python fixbuffer.py --restore  put the backup back

NOT APPLIED. This on-disk patch shipped in client 0.6.7 and was withdrawn in
0.6.8: a modified Napoleon.exe hides the public multiplayer lobbies, because
the game or Steam fingerprints the exe file and a patched game only sees other
patched games. The client never offers apply() any more. It still calls
status() at every launch and, if it finds an exe patched by 0.6.7, puts the
stock exe back from the backup (asking the player to close the game first if
it is running) and says so. The same fix is now applied to the running game's
memory instead (artilleryfix.py), which imports SITES, cave_for() and rel32()
from here; that is why this module is still shipped.

The bug: when a gun picks its own target the engine copies every enemy unit in
its targeting region into a buffer of 100 pointers on the stack, with the
count stored right after slot 100. Nine append sites write buf[count++] and
none checks the count, so the 101st unit overwrites the count and the game
dies with an access violation.

What apply() would change: each of the nine sites becomes a jump into a small
cave that skips the store and the increment when count >= 100 and otherwise
runs the original instructions. The caves live in a new 512-byte section
(.rdcz) appended to the exe, so no existing byte moves; the exe grows from
17,895,424 to 17,895,936 bytes. apply() requires the stock Napoleon.exe
1.3.0.0 by size and SHA-256 (STOCK_SIZE, STOCK_SHA), copies it to
redcoatz_backup/Napoleon.exe with a README.txt, writes the patched file
through a temporary file, and requires the result to hash to PATCHED_SHA;
otherwise the backup is copied back. restore() copies the backup back only if
it hashes to STOCK_SHA. Steam's "Verify integrity of game files" also
restores the original.
"""

import argparse
import hashlib
import os
import shutil
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

EXE_NAME = "Napoleon.exe"
BACKUP_DIR = "redcoatz_backup"

STOCK_SIZE = 17895424
STOCK_SHA = "44880ec1770f600163a336eec83b4366901484c5cceec83dc326e2932f02a054"
# The patcher refuses to call a result "patched" unless it hashes to this.
PATCHED_SHA = "df7b1452c8817572f851ed238f9cfe77212c6616d57ac39f6c48e3ffeb9ef282"
PATCHED_SIZE = STOCK_SIZE + 0x200

IMAGE_BASE = 0x400000
TEXT_RVA, TEXT_RAW = 0x1000, 0x400          # file offset = rva - (TEXT_RVA - TEXT_RAW)
TEXT_DELTA = TEXT_RVA - TEXT_RAW

# The new section.
SEC_NAME = b".rdcz\0\0\0"
SEC_RVA = 0x14EB000                         # first page after .reloc (0x1462000 + 0x88a64, up), = stock SizeOfImage
SEC_RAW = STOCK_SIZE                        # appended at the end of the file
SEC_SIZE = 0x200
SEC_CHARS = 0x60000020                      # CODE | EXECUTE | READ
STOCK_SIZEOFIMAGE = 0x14EB000


def header_offsets(img):
    """(NumberOfSections offset, SizeOfImage offset, end of the section table),
    read from the DOS header's e_lfanew rather than assumed."""
    pe = struct.unpack_from("<I", img, 0x3C)[0]
    assert img[pe:pe + 4] == b"PE" + bytes(2), "not a PE file"
    nsec = struct.unpack_from("<H", img, pe + 6)[0]
    opt_size = struct.unpack_from("<H", img, pe + 20)[0]
    assert opt_size == 0xE0, "not a PE32 optional header"
    return pe + 6, pe + 24 + 56, pe + 24 + opt_size + nsec * 40

LIMIT = 100

# Register numbers for ModRM.
REG = {"eax": 0, "ecx": 1, "edx": 2, "ebx": 3, "esp": 4, "ebp": 5, "esi": 6, "edi": 7}

#: The nine append sites: rva, original bytes, base register of the buffer,
#: the register the count is read into, the register that gets count+1.
#: Sites 7-9 arrive with the count already in edx and no load of their own.
SITES = [
    (0x2C9D05, "8b8d900100008d410189859001000089748d00", "ebp", "ecx", "eax"),
    (0x2C9DF4, "8b8f900100008d4101898790010000892c8f", "edi", "ecx", "eax"),
    (0x2C9F67, "8b8e900100008d4101898690010000893c8e", "esi", "ecx", "eax"),
    (0x301187, "8b97900100008d4a01898f90010000892c97", "edi", "edx", "ecx"),
    (0x301238, "8b8b900100008d410189839001000089348b", "ebx", "ecx", "eax"),
    (0x3012E0, "8b96900100008d4a01898e90010000892c96", "esi", "edx", "ecx"),
    (0x3013F6, "8d4a01898f90010000890497", "edi", "edx", "ecx"),
    (0x301597, "8d4a01898f90010000890497", "edi", "edx", "ecx"),
    (0x3018D4, "8d4a01898f90010000890497", "edi", "edx", "ecx"),
]


def sha256(path):
    """SHA-256 of a file, read in blocks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def locate():
    """Full path to Napoleon.exe, or None."""
    try:
        import gamelaunch
        launcher = gamelaunch.find_launcher()
    except Exception:
        launcher = None
    cands = []
    if launcher:
        # ...\data\NTW3\Launcher\NTW3 v9.6.exe -> game dir is three up from data
        d = os.path.dirname(launcher)
        for _ in range(4):
            d = os.path.dirname(d)
            cands.append(os.path.join(d, EXE_NAME))
    cands.append(r"C:\Program Files (x86)\Steam\steamapps\common\Napoleon Total War\Napoleon.exe")
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def backup_path(path):
    """Where the original is kept: redcoatz_backup/Napoleon.exe beside the exe."""
    return os.path.join(os.path.dirname(path), BACKUP_DIR, EXE_NAME)


# --------------------------------------------------------------------------
# Building the patched image


def modrm_disp32(reg, base):
    """ModRM byte for [base + disp32] with the given reg field."""
    return bytes([0x80 | (reg << 3) | base])


def rel32(d):
    """A jump distance as rel32. The process is 32-bit, so EIP wraps at 4 GB
    and any two addresses are one rel32 apart; that keeps the encoding valid
    for a cave anywhere in the process (artilleryfix.py)."""
    return struct.pack("<i", ((d + 0x80000000) & 0xFFFFFFFF) - 0x80000000)


def cave_for(rva, orig, base, count, plus, cave_rva):
    """The cave bytes for one site, and the site's replacement bytes."""
    b, c, p = REG[base], REG[count], REG[plus]
    back = rva + len(orig)                      # where the site's code continues
    out = bytearray()
    if len(orig) == 12:
        out += b"\x83\xFA" + bytes([LIMIT])     # cmp edx, 100
    else:
        out += b"\x83" + modrm_disp32(7, b) + struct.pack("<I", 0x190) + bytes([LIMIT])   # cmp [base+0x190], 100
    jae_at = len(out)
    out += b"\x73\x00"                          # jae full (patched below)
    out += orig                                 # the original instructions
    out += b"\xE9" + rel32(back - (cave_rva + len(out) + 5))                 # jmp back
    full = len(out)
    out[jae_at + 1] = full - (jae_at + 2)
    if len(orig) == 12:
        out += b"\x8D\x4A\x01"                  # lea ecx, [edx+1]
    else:
        out += b"\x8B" + modrm_disp32(c, b) + struct.pack("<I", 0x190)                   # mov count, [base+0x190]
        out += b"\x8D" + bytes([0x40 | (p << 3) | c]) + b"\x01"                          # lea plus, [count+1]
    out += b"\xE9" + rel32(back - (cave_rva + len(out) + 5))                 # jmp back
    site = b"\xE9" + rel32(cave_rva - (rva + 5)) + b"\x90" * (len(orig) - 5)
    return bytes(out), site


def build(stock):
    """The patched image from the stock bytes. Pure: no files touched."""
    assert len(stock) == STOCK_SIZE
    img = bytearray(stock)
    # 1. section header
    nsec_off, soi_off, table_end = header_offsets(img)
    assert struct.unpack_from("<H", img, nsec_off)[0] == 6, "expected six sections"
    assert img[table_end:table_end + 40] == bytes(40), "section table slot not empty"
    hdr = struct.pack("<8sIIIIIIHHI", SEC_NAME, SEC_SIZE, SEC_RVA, SEC_SIZE, SEC_RAW, 0, 0, 0, 0, SEC_CHARS)
    img[table_end:table_end + 40] = hdr
    struct.pack_into("<H", img, nsec_off, 7)
    assert struct.unpack_from("<I", img, soi_off)[0] == STOCK_SIZEOFIMAGE, "unexpected SizeOfImage"
    struct.pack_into("<I", img, soi_off, STOCK_SIZEOFIMAGE + 0x1000)
    # 2. caves and sites
    caves = bytearray()
    for rva, hexorig, base, count, plus in SITES:
        orig = bytes.fromhex(hexorig)
        off = rva - TEXT_DELTA
        assert bytes(img[off:off + len(orig)]) == orig, f"site {rva:#x}: bytes differ from stock"
        cave_rva = SEC_RVA + len(caves)
        cave, site = cave_for(rva, orig, base, count, plus, cave_rva)
        caves += cave
        img[off:off + len(orig)] = site
    assert len(caves) <= SEC_SIZE
    img += bytes(caves) + bytes(SEC_SIZE - len(caves))
    assert len(img) == PATCHED_SIZE
    return bytes(img)


def diff(stock, patched):
    """[(file offset, old bytes, new bytes)] for everything that changed."""
    out, i, n = [], 0, min(len(stock), len(patched))
    while i < n:
        if stock[i] != patched[i]:
            j = i
            while j < n and stock[j] != patched[j]:
                j += 1
            out.append((i, stock[i:j], patched[i:j]))
            i = j
        else:
            i += 1
    if len(patched) > n:
        out.append((n, b"", patched[n:]))
    return out


# --------------------------------------------------------------------------
# Status / apply / restore

STOCK, FIXED, UNKNOWN, MISSING = "stock", "fixed", "unknown", "missing"


def status(path=None):
    """One of stock / fixed / unknown / missing, with the file's sha256."""
    path = path or locate()
    if not path or not os.path.isfile(path):
        return {"state": MISSING, "path": path, "detail": "Napoleon.exe not found"}
    size = os.path.getsize(path)
    digest = sha256(path)
    if size == STOCK_SIZE and digest == STOCK_SHA:
        return {"state": STOCK, "path": path, "sha256": digest,
                "detail": "stock Napoleon.exe, the 100-unit buffer is unbounded"}
    if size == PATCHED_SIZE and digest == PATCHED_SHA:
        return {"state": FIXED, "path": path, "sha256": digest,
                "detail": "Napoleon.exe already carries the buffer bound"}
    return {"state": UNKNOWN, "path": path, "sha256": digest, "size": size,
            "detail": "not the stock 2024 exe and not our patched one"}


def apply(path=None, log=print):
    """Back up, patch, verify. Returns (ok, message).

    Not called by the client any more; see the module docstring.
    """
    path = path or locate()
    st = status(path)
    if st["state"] == FIXED:
        return True, "already patched"
    if st["state"] != STOCK:
        return False, f"refusing: {st.get('detail', st['state'])} ({st.get('sha256', '')[:16]})"
    with open(path, "rb") as f:
        stock = f.read()
    patched = build(stock)
    got = hashlib.sha256(patched).hexdigest()
    if got != PATCHED_SHA:
        return False, f"refusing: the build does not hash to the tested result ({got[:16]})"
    bak = backup_path(path)
    os.makedirs(os.path.dirname(bak), exist_ok=True)
    shutil.copy2(path, bak)
    if sha256(bak) != STOCK_SHA:
        return False, "backup copy failed to verify; nothing changed"
    with open(os.path.join(os.path.dirname(bak), "README.txt"), "w", encoding="utf-8") as f:
        f.write(
            f"Napoleon.exe backup made by redcoatz fixbuffer.py on {time.strftime('%Y-%m-%d %H:%M')}.\n"
            f"This is the STOCK exe, sha256 {STOCK_SHA}, {STOCK_SIZE} bytes.\n"
            f"The live Napoleon.exe was patched to bound a 100-unit buffer whose\n"
            f"overrun crashes big battles (sha256 {PATCHED_SHA}, {PATCHED_SIZE} bytes).\n"
            f"To undo: copy this file over Napoleon.exe, or run fixbuffer.py --restore,\n"
            f"or verify the game files in Steam.\n")
    tmp = path + ".redcoatz.tmp"
    with open(tmp, "wb") as f:
        f.write(patched)
    if sha256(tmp) != PATCHED_SHA:
        os.remove(tmp)
        return False, "written file failed to verify; nothing changed"
    try:
        os.replace(tmp, path)
    except PermissionError:
        os.remove(tmp)
        return False, "Napoleon.exe is in use (the game is still running); nothing changed"
    if sha256(path) != PATCHED_SHA:
        shutil.copy2(bak, path)
        return False, "exe failed to verify after writing; backup restored"
    return True, f"patched: {len(SITES)} sites bounded, caves in a new {SEC_SIZE}-byte section; backup at {bak}"


def restore(path=None):
    """Copy the backup over Napoleon.exe if it is the stock exe. Returns (ok, message)."""
    path = path or locate()
    bak = backup_path(path) if path else None
    if not bak or not os.path.isfile(bak):
        return False, "no backup found"
    if sha256(bak) != STOCK_SHA:
        return False, "backup is not the stock exe; not restoring from it"
    try:
        shutil.copy2(bak, path)
    except PermissionError:
        return False, "Napoleon.exe is in use (the game is still running); close it and try again"
    return sha256(path) == STOCK_SHA, "restored"


def main():
    """Command-line entry point: dry-run report, --apply or --restore."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--path", help="Napoleon.exe to use instead of the installed one")
    args = ap.parse_args()
    path = args.path or locate()
    if args.restore:
        ok, msg = restore(path)
        print(msg)
        return 0 if ok else 1
    if args.apply:
        ok, msg = apply(path)
        print(msg)
        return 0 if ok else 1
    st = status(path)
    print(f"{st['state']}: {st.get('path')}  {st.get('sha256', '')[:16]}  {st.get('detail', '')}")
    if st["state"] == STOCK:
        with open(path, "rb") as f:
            stock = f.read()
        patched = build(stock)
        print(f"dry run: would write {len(patched)} bytes, sha256 {hashlib.sha256(patched).hexdigest()}")
        for off, old, new in diff(stock, patched):
            what = "appended section" if not old else ("section header / count / image size" if off < 0x400 else "site")
            print(f"   file 0x{off:07X}  {len(new):3d} bytes  {what}  {old.hex()[:40]} -> {new.hex()[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
