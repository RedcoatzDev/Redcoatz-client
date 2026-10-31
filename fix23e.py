"""Repair the NTW3 "23e bug" in the player's variants94b.pack.

    python fix23e.py            report what state the pack is in
    python fix23e.py --apply    back up, patch, verify
    python fix23e.py --restore  put the backup back

The bug: the 23e leger's flag-bearer variant asks for four textures that are
registered later in the load than the moment it is resolved. The engine's
name lookup returns 0xFFFF for "not found" and uses it as an array index
without a bounds check, which can crash the battle.

What is changed: one file,

    <game>/data/NTW3/Version94/Graphics/variants94b.pack   (422 MB)

Four UTF-16 texture paths in each of six flag-bearer entries are repointed at
textures that are already loaded (24 paths, 1,344 bytes written, 666 bytes
actually different). Every replacement is the same length or shorter and is
written over the existing NUL-padded field, so the file size, every entry
offset and the index stay byte-identical. Nothing else in the pack changes.

Safety: apply() first requires the whole file to have the exact size and
SHA-256 of the stock 2023 pack (PACK_SIZE, STOCK_PACK_SHA) and declines any
other file. It copies the original to redcoatz_backup/variants94b.pack next
to the pack, with a README.txt explaining the change, writes the patch, then
requires the result to hash to FIXED_PACK_SHA; otherwise it copies the backup
back. The client (ntw3ladder.py) calls apply() only after the player has
agreed in a dialog that names this fix; declining closes the client without
changing anything.

Undo: --restore copies the backup back, or copy it over the pack by hand.
The NTW3 launcher updates its own packs, so a mod release overwrites the
repair and the client offers it again.

Which entries are repaired is decided by content, not by name: 24 entries in
the pack are the same 11,426-byte flag-bearer file under different names, and
only the six whose soldier variant does not itself reference the missing
textures actually fail the lookup (see _selected). On the 2023 pack that rule
selects exactly six entries, which is where FIXED_PACK_SHA comes from.
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

PACK_REL = os.path.join("NTW3", "Version94", "Graphics", "variants94b.pack")
BACKUP_DIR = "redcoatz_backup"

#: The faulty flag-bearer variant, identified by its contents: 24 entries in
#: variants94b.pack are this exact 11,426-byte file under different names.
BLOB_SIZE = 11426
FAULTY_SHA = "df8f854aae03f2642461c32e7790c81a76b20c32e4a0c2a016852d65950ab2a0"
FIXED_SHA = "0454be8460b244adb0a1d2b31c857bf3ee94d07dc4e7b874fffb167676d1ffaa"

# The whole file, before and after. Only the known stock pack is patched and
# only the known result is accepted.
PACK_SIZE = 422555753
STOCK_PACK_SHA = "4d1f8e5d517767930a1aa63f1a779806516c2f9428c184d4f9ad89420f82903b"
FIXED_PACK_SHA = "4eb714d3d807911c66e3f34151637e9d679fc5a673a49c8565a968c0c7e40fa9"

_P = "VariantModels/UnitParts/Euro/"
FIXES = (
    (_P + "_skinparts_austrian/tex1_is/texture",
     _P + "_skinparts_french/tex1_is/texture"),
    (_P + "Hats/Colpack/tex/texture",
     _P + "Hats/Bicorn/tex/texture"),
    (_P + "Torsos/A_I_Coat/Austrian_General_Coat/texture",
     _P + "belts/Belts/Belt_2/texture"),
)

STOCK, FIXED, UNKNOWN, MISSING = "stock", "fixed", "unknown", "missing"


def locate():
    """Full path to variants94b.pack, or None."""
    try:
        import gamelaunch
        launcher = gamelaunch.find_launcher()
    except Exception:
        launcher = None
    if launcher:
        # ...\data\NTW3\Launcher\NTW3 v9.6.exe -> ...\data
        data = os.path.dirname(os.path.dirname(os.path.dirname(launcher)))
        p = os.path.join(data, PACK_REL)
        if os.path.isfile(p):
            return p
    return None


def _index(f):
    """[(name, offset, size)] for a .pack.

    The header length varies with the pack version (PFH0 starts the index at
    0x18, PFH3 at 0x20) and guessing wrong gives plausible names with wrong
    sizes, so the start is probed and accepted only when the index is consumed
    to exactly its stated length.
    """
    f.seek(0)
    head = f.read(40)
    if head[:3] != b"PFH":
        raise ValueError(f"not a pack ({head[:4]!r})")
    deps, count, idx_size = struct.unpack_from("<III", head, 12)

    def parse(start, flagged):
        f.seek(start + deps)
        table = f.read(idx_size)
        if len(table) < idx_size:
            return None
        out, p, cursor = [], 0, start + deps + idx_size
        for _ in range(count):
            if p + 5 > len(table):
                return None
            size = struct.unpack_from("<I", table, p)[0]
            p += 4
            if flagged:
                p += 1
            end = table.find(b"\x00", p)
            if end < 0:
                return None
            out.append((table[p:end].decode("latin1")
                        .replace("\\", "/").lower(), cursor, size))
            cursor += size
            p = end + 1
        return out if p == idx_size else None

    for start in (24, 28, 32, 36):
        for flagged in (False, True):
            got = parse(start, flagged)
            if got is not None:
                return got
    raise ValueError("could not locate the pack index")


def _bad_refs(blob):
    """How many of the three known-bad textures this variant asks for."""
    return sum(blob.count(a.encode("utf-16-le")) for a, _ in FIXES)


def _selected(f):
    """The flag-bearer entries this tool will repair.

    Both conditions are needed: the entry is byte-for-byte the known faulty
    flag-bearer file, and the same unit's soldier variant asks for none of the
    bad textures. Where the soldier asks for them too it is dressed first, the
    textures are registered early and the flag bearer's lookup succeeds; only
    the units with a clean soldier crash, and only they are repaired.
    """
    idx = _index(f)
    by_name = {n: (off, size) for n, off, size in idx}
    out = []
    for name, off, size in idx:
        if size != BLOB_SIZE or not name.endswith(
                ".standard_bearer.unit_variant"):
            continue
        f.seek(off)
        blob = f.read(size)
        digest = hashlib.sha256(blob).hexdigest()
        if digest not in (FAULTY_SHA, FIXED_SHA):
            continue
        soldier = by_name.get(
            name.replace(".standard_bearer.", ".soldier."))
        if not soldier:
            continue
        f.seek(soldier[0])
        if _bad_refs(f.read(soldier[1])):
            continue                 # the soldier registers it; leave alone
        out.append((name, off, size, digest))
    return out


def status(path=None):
    """One of stock / fixed / unknown / missing, plus detail for the caller."""
    path = path or locate()
    if not path or not os.path.isfile(path):
        return {"state": MISSING, "path": path, "detail": "pack not found"}
    try:
        with open(path, "rb") as f:
            sel = _selected(f)
    except Exception as e:                    # noqa: BLE001
        return {"state": UNKNOWN, "path": path, "detail": str(e)}

    old = sum(1 for *_, d in sel if d == FAULTY_SHA)
    new = sum(1 for *_, d in sel if d == FIXED_SHA)
    if not sel:
        state, detail = UNKNOWN, "no repairable entries found in this pack"
    elif old and not new:
        state, detail = STOCK, f"{old} unrepaired flag bearer(s)"
    elif new and not old:
        state, detail = FIXED, f"{new} flag bearer(s) already repaired"
    else:
        state, detail = UNKNOWN, f"{old} unrepaired, {new} repaired -- mixed"
    return {"state": state, "path": path, "old": old, "new": new,
            "names": [n for n, *_ in sel], "detail": detail}


def _patch_blob(blob):
    """Rewrite the paths in place. Returns (bytes, count)."""
    out, n = bytearray(blob), 0
    for a, b in FIXES:
        ab, bb = a.encode("utf-16-le"), b.encode("utf-16-le")
        if len(bb) > len(ab):
            raise ValueError(f"replacement longer than original: {b}")
        at = 0
        while True:
            i = out.find(ab, at)
            if i < 0:
                break
            out[i:i + len(ab)] = bb + b"\x00" * (len(ab) - len(bb))
            n += 1
            at = i + len(bb)
    return bytes(out), n


def _sha(path, blocks=1 << 20):
    """SHA-256 of a file, read in blocks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(blocks), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_path(path):
    """Where the original is kept: redcoatz_backup/<name> beside the pack."""
    return os.path.join(os.path.dirname(path), BACKUP_DIR,
                        os.path.basename(path))


def _write_backup_readme(pack, bak, sha, names, changed):
    """Write a README.txt beside the backup saying what it is, what was
    changed and how to restore it without the client."""
    # copy2 keeps the pack's own mtime, so this is the date the mod shipped it.
    made = time.strftime("%d %B %Y", time.localtime(os.path.getmtime(bak)))
    now = time.strftime("%d %B %Y")
    n = len(names)
    plural = "y" if n == 1 else "ies"
    listing = "\n".join("    " + x.rsplit("/", 1)[-1] for x in names)
    text = f"""\
variants94b.pack -- ORIGINAL, UNMODIFIED
========================================

This is the original pack provided by the NTW3 mod team. The Redcoatz NTW3
Ladder client copied it here before repairing {n} unit entr{plural} in the
working copy, rewriting {changed:,} bytes, to fix the "23e bug".

    file dated    {made}
    backed up on  {now}
    size          {os.path.getsize(bak):,} bytes
    sha256        {sha}

The repaired working copy is the same size and has this sha256:

    {FIXED_PACK_SHA}

The client checked both of those before and after it wrote anything. If the
result had not matched, it would have put this backup back automatically.


To restore the original
-----------------------

Close NTW3 and its launcher, then copy this file over:

    {pack}

That is all. The file is byte-for-byte what the mod shipped, so replacing the
working copy with it puts your install back exactly as it was.


What was changed, and why
-------------------------

Four texture paths in each of these flag-bearer entries:

{listing}

They all hold byte-identical contents, and they all ask for textures the game
has not registered yet at the moment it looks them up. The lookup returns
"not found",
and Napoleon.exe uses that answer as an array index without checking it --
reading a long way past the end of a small table and building a pointer out of
whatever it finds. Most of the time that is harmless and the soldier just looks
slightly wrong. Sometimes it crashes the battle.

The four paths were repointed at textures that are already loaded at that
moment, so the failed lookup no longer happens:

    _skinparts_austrian/tex1_is/texture   x2 -> _skinparts_french/tex1_is/texture
    Hats/Colpack/tex/texture                 -> Hats/Bicorn/tex/texture
    Torsos/A_I_Coat/Austrian_General_Coat/.. -> belts/Belts/Belt_2/texture

Nothing else in the pack was touched: same file size, same index, same offsets.
The 23e's flag bearers wear a slightly different hat and belt as a result.


Notes
-----

The NTW3 launcher updates its own packs. A mod release will replace the working
copy and undo the repair -- the client will simply offer it again.

This folder can be deleted if you no longer want the ability to undo the
change. Nothing else uses it.
"""
    try:
        with open(os.path.join(os.path.dirname(bak), "README.txt"), "w",
                  encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass          # the backup itself matters; this note does not


def apply(path=None, log=print):
    """Back up, patch, verify. Restores the backup if anything is off.

    Returns (ok, message).
    """
    st = status(path)
    path = st["path"]
    if st["state"] == FIXED:
        return True, "Already fixed"
    if st["state"] != STOCK:
        return False, f"Not patching: {st['detail']}"

    # The authoritative identity check. status() only reads the index and a
    # few entries, which keeps it cheap enough for every launch; the whole file
    # is hashed once, here, just before writing.
    size_before = os.path.getsize(path)
    before = _sha(path)
    log(f"  sha256 before {before}")
    if size_before != PACK_SIZE or before != STOCK_PACK_SHA:
        return False, ("This is not the 2023 variants94b.pack this fix was "
                       "built and tested against, so nothing was changed. "
                       f"Expected {PACK_SIZE:,} bytes / {STOCK_PACK_SHA[:16]}"
                       f"..., found {size_before:,} bytes / {before[:16]}...")

    bak = backup_path(path)
    try:
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        if not os.path.exists(bak):
            log(f"  backing up -> {bak}")
            shutil.copy2(path, bak)
    except OSError as e:
        return False, f"Could not write a backup: {e}"

    try:
        with open(path, "r+b") as f:
            plan, changed = [], 0
            for name, off, size, digest in _selected(f):
                if digest != FAULTY_SHA:
                    continue
                f.seek(off)
                was = f.read(size)
                blob, n = _patch_blob(was)
                if n:
                    plan.append((name, off, blob))
                    changed += sum(1 for a, b in zip(was, blob) if a != b)
            # After the plan is built, so the note states what was actually
            # repaired.
            _write_backup_readme(path, bak, before,
                                 [q[0] for q in plan], changed)
            for name, off, blob in plan:
                log(f"  repairing {name.rsplit('/', 1)[-1]}")
                f.seek(off)
                f.write(blob)
    except PermissionError:
        return False, ("No permission to write in the game folder. "
                       "Run the client as administrator and try again.")
    except Exception as e:
        _restore(path, bak, log)
        return False, f"Patch failed, original restored: {e}"

    # The result must be the one known file, byte for byte; anything else is
    # undone.
    after = _sha(path)
    log(f"  sha256 after  {after}")
    if os.path.getsize(path) != size_before or after != FIXED_PACK_SHA:
        _restore(path, bak, log)
        return False, ("Verification failed, original restored (expected "
                       f"{FIXED_PACK_SHA[:16]}..., got {after[:16]}...)")
    return True, (f"23e bug fixed: repaired {len(plan)} unit "
                  f"entr{'y' if len(plan) == 1 else 'ies'}, "
                  f"{changed:,} bytes changed, file size unchanged")


def _restore(path, bak, log=print):
    """Copy the backup over the pack, if there is one."""
    if os.path.exists(bak):
        log("  restoring the backup")
        shutil.copy2(bak, path)


def restore(path=None, log=print):
    """Put the backup back. Returns (ok, message)."""
    st = status(path)
    path = st["path"]
    bak = backup_path(path)
    if not os.path.exists(bak):
        return False, "No backup to restore"
    try:
        shutil.copy2(bak, path)
    except OSError as e:
        return False, f"Could not restore: {e}"
    return True, "Original pack restored"


def main():
    """Command-line entry point: report, --apply or --restore."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", help="path to variants94b.pack")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()

    st = status(args.pack)
    print(f"  pack : {st['path']}")
    print(f"  state: {st['state']}  ({st['detail']})")
    if st["state"] in (STOCK, FIXED) and st["path"]:
        print(f"  sha256: {_sha(st['path'])}")

    if args.apply:
        ok, msg = apply(args.pack)
        print(f"\n  {'OK' if ok else 'FAILED'}: {msg}")
        return 0 if ok else 1
    if args.restore:
        ok, msg = restore(args.pack)
        print(f"\n  {'OK' if ok else 'FAILED'}: {msg}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
