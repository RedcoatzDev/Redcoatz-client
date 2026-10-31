"""Repair a crash in the game's sound library, mss32.dll.

    python fixmiles.py            report what state the DLL is in
    python fixmiles.py --apply    back up, patch, verify
    python fixmiles.py --restore  put the backup back

The bug: Napoleon passes Miles Sound System the number of samples left in a
sound, which is zero at the moment a sound finishes. When the mixer is holding
samples over from its previous call it skips the zero check and enters a loop
that decrements before it tests, and that loop stops only on exactly zero. The
count becomes minus one, the loop reads past the end of the sound until it
reaches unmapped memory, and the game closes silently: no dialog, no desync
file, no replay.

What is changed: three bytes in <game root>/mss32.dll (449,024 bytes). Each
is the opcode of a conditional jump, changed from "equal to zero" to "zero or
below" (see FIXES). No code moves and the file size is unchanged. The patched
loop still reads one sample (two bytes) past the end of a sound; in every
layout observed that is inside the mixer's own pool, and removing it would
need more than a one-byte patch.

Safety: apply() requires the file to have the exact size and SHA-256 of the
DLL Steam ships (DLL_SIZE, STOCK_SHA) and checks the original bytes at each
site before writing. It copies the original to redcoatz_backup/mss32.dll
beside the DLL with a README.txt, writes the patched file, then requires it to
hash to FIXED_SHA; otherwise the backup is copied back. The client
(ntw3ladder.py) calls apply() only after the player has agreed in a dialog
that names this fix; declining closes the client without changing anything.

Undo: --restore copies the backup back, or copy it by hand, or use Steam's
"Verify integrity of game files", which restores the original too (the client
then offers the fix again).

The fix was verified by running the routine under emulation against the real
DLL, driven the way the game's mixer drives it, with 69 real PCM sounds; see
soundfix/ in the repository.
"""

import argparse
import hashlib
import os
import shutil
import struct
import sys
import time

#: The sound library, in the game's root folder.
DLL_NAME = "mss32.dll"

#: Shared with fix23e: one backup folder per install.
BACKUP_DIR = "redcoatz_backup"

#: Miles Sound System 7.2.6.0 as Steam ships it with Napoleon: Total War.
STOCK_SHA = "d7a73a1b0e005d0f4ce1437c13196b9967c801d5cbc0c8ebcde8977d265f55fd"
#: The same file with the three bytes below changed, and nothing else.
FIXED_SHA = "2e5c043c428543433e9de77863be7f8c510c28f5a77580bc54910190e5aa9908"
DLL_SIZE = 449024

#: rva -> (bytes that must be there, replacement, description). RVAs become
#: file offsets by reading the PE headers; the bytes are checked before each
#: write and the whole file after.
FIXES = {
    0x2E960: (b"\x0f\x85", b"\x0f\x8f", "jne -> jg"),
    0x2E97D: (b"\x0f\x84", b"\x0f\x8e", "je -> jle"),
    0x2EA50: (b"\x0f\x84", b"\x0f\x8e", "je -> jle"),
}

STOCK, FIXED, UNKNOWN, MISSING = "stock", "fixed", "unknown", "missing"


def locate():
    """Full path to the game's mss32.dll, or None."""
    try:
        import gamelaunch
        launcher = gamelaunch.find_launcher()
    except Exception:                        # noqa: BLE001 - optional import
        launcher = None
    if launcher:
        # ...\data\NTW3\Launcher\NTW3 v9.6.exe -> ...\data -> the game root
        data = os.path.dirname(os.path.dirname(os.path.dirname(launcher)))
        p = os.path.join(os.path.dirname(data), DLL_NAME)
        if os.path.isfile(p):
            return p
    return None


def _sha(path, blocks=1 << 20):
    """SHA-256 of a file, read in blocks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(blocks), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_path(path):
    """Where the original is kept: redcoatz_backup/mss32.dll beside the DLL."""
    return os.path.join(os.path.dirname(path), BACKUP_DIR,
                        os.path.basename(path))


def _rva_to_off(b, rva):
    """Where an address lives in the file, read from the PE headers."""
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    nsec = struct.unpack_from("<H", b, pe + 6)[0]
    optsz = struct.unpack_from("<H", b, pe + 20)[0]
    for i in range(nsec):
        o = pe + 24 + optsz + i * 40
        vs, va, _rs, ra = struct.unpack_from("<IIII", b, o + 8)
        if va <= rva < va + vs:
            return ra + (rva - va)
    raise ValueError("rva %#x is in no section" % rva)


def status(path=None):
    """Which of the four states the installed DLL is in (hashes the whole file)."""
    path = path or locate()
    if not path or not os.path.isfile(path):
        return {"path": path, "state": MISSING,
                "detail": "mss32.dll not found"}
    size = os.path.getsize(path)
    if size != DLL_SIZE:
        return {"path": path, "state": UNKNOWN,
                "detail": "%s bytes, expected %s" % (f"{size:,}",
                                                     f"{DLL_SIZE:,}")}
    sha = _sha(path)
    if sha == FIXED_SHA:
        return {"path": path, "state": FIXED, "sha": sha,
                "detail": "already fixed"}
    if sha == STOCK_SHA:
        return {"path": path, "state": STOCK, "sha": sha,
                "detail": "stock Miles 7.2.6.0, has the crash"}
    return {"path": path, "state": UNKNOWN, "sha": sha,
            "detail": "unrecognised build (%s...)" % sha[:16]}


def _readme_text(dll, bak):
    """Text of the README.txt kept beside the backup."""
    made = time.strftime("%d %B %Y", time.localtime(os.path.getmtime(bak)))
    now = time.strftime("%d %B %Y")
    changes = "\n".join("    0x%06X   %s" % (rva, FIXES[rva][2])
                        for rva in sorted(FIXES))
    return """\
mss32.dll -- ORIGINAL, UNMODIFIED
=================================

This is the sound library Steam ships with Napoleon: Total War, dated %s
and copied here on %s by the Redcoatz NTW3 Ladder client before it changed
three bytes in the copy the game loads.

WHY
    Miles Sound System has a crash that closes the game with no warning and
    no error message. When a sound finishes at the same moment the mixer is
    holding samples over from its previous call, a loop that should stop
    runs on instead, reading past the end of the sound until it reaches
    memory that is not there.

WHAT CHANGED
    Three bytes, at these addresses inside the file:

%s

    Each is one conditional jump. Each asked "is the count exactly zero?"
    and now asks "is it zero or below?". Nothing moved; the patched file is
    the same size as this one.

TO UNDO IT
    Copy this file over %s in the folder above, or in Steam right-click
    Napoleon: Total War, Properties, Installed Files, Verify integrity of
    game files. Either restores the original exactly.

    Verifying in Steam ALSO undoes it by accident. Nothing breaks if that
    happens -- the client notices and offers the fix again.

IS IT SAFE TO DELETE THIS FOLDER?
    Yes. Steam can always restore the original. Keeping it just makes it
    quicker, and lets you undo the change without the client installed.
""" % (made, now, changes, os.path.basename(dll))


def apply(path=None, log=print):
    """Back up, patch, verify. Restores the backup if anything is off.

    Returns (ok, message).
    """
    st = status(path)
    path = st["path"]
    if st["state"] == FIXED:
        return True, "The sound fix is already applied."
    if st["state"] != STOCK:
        return False, "Not patching: %s" % st["detail"]

    with open(path, "rb") as f:
        b = bytearray(f.read())
    # Check every site before writing any: a file that half matches is not
    # understood, and half a patch is worse than none.
    for rva in sorted(FIXES):
        want = FIXES[rva][0]
        o = _rva_to_off(b, rva)
        if bytes(b[o:o + len(want)]) != want:
            return False, ("The bytes at 0x%X are not what this fix was built "
                           "against, so nothing was changed." % rva)
    for rva in sorted(FIXES):
        _want, fix, name = FIXES[rva]
        o = _rva_to_off(b, rva)
        b[o:o + len(fix)] = fix
        log("  0x%06X  %s" % (rva, name))

    bak = backup_path(path)
    try:
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        if not os.path.exists(bak):
            log("  backing up -> %s" % bak)
            shutil.copy2(path, bak)
            with open(os.path.join(os.path.dirname(bak), "README.txt"), "w",
                      encoding="utf-8") as f:
                f.write(_readme_text(path, bak))
        with open(path, "wb") as f:
            f.write(bytes(b))
    except OSError as e:
        _restore(path, bak, log)
        return False, ("Could not write to the game folder, so nothing was "
                       "changed (%s). Running the client as administrator "
                       "would let it." % (e.strerror or e))

    after = _sha(path)
    if after != FIXED_SHA:
        log("  sha256 after %s -- WRONG" % after)
        _restore(path, bak, log)
        return False, ("The patched file did not come out as expected, so the "
                       "original was put back and nothing was changed.")
    log("  sha256 after %s" % after)
    return True, ("Sound crash fixed: three bytes changed in mss32.dll. The "
                  "original is kept in %s, and Steam's Verify integrity of "
                  "game files will restore it at any time." % BACKUP_DIR)


def _restore(path, bak, log=print):
    """Copy the backup over the DLL, if there is one."""
    if os.path.exists(bak):
        log("  restoring the backup")
        shutil.copy2(bak, path)


def restore(path=None, log=print):
    """Put the backup back. Returns (ok, message)."""
    st = status(path)
    path = st["path"]
    if not path:
        return False, "mss32.dll not found"
    bak = backup_path(path)
    if not os.path.exists(bak):
        return False, "No backup to restore"
    try:
        shutil.copy2(bak, path)
    except OSError as e:
        return False, "Could not restore: %s" % e
    return True, "Original mss32.dll restored"


def main():
    """Command-line entry point: report, --apply or --restore."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", help="path to mss32.dll")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()

    st = status(args.dll)
    print("  dll  : %s" % st["path"])
    print("  state: %s  (%s)" % (st["state"], st["detail"]))
    if st.get("sha"):
        print("  sha256: %s" % st["sha"])

    if args.apply:
        ok, msg = apply(args.dll)
        print("\n  %s: %s" % ("OK" if ok else "FAILED", msg))
        return 0 if ok else 1
    if args.restore:
        ok, msg = restore(args.dll)
        print("\n  %s: %s" % ("OK" if ok else "FAILED", msg))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
