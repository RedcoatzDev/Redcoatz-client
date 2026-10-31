"""Fix the large artillery crash in the running game, in memory only.

    python artilleryfix.py            show the state of the nine sites
    python artilleryfix.py --apply    patch the running game (main menu or lobby)
    python artilleryfix.py --undo     put the original code back

The bug: when a gun chooses its own target the engine copies every enemy unit
in its targeting area into a list of 100 on the stack, and nine places append
to it without checking the count. The 101st unit overwrites the count and the
game crashes. The sites and the check are defined in fixbuffer.py.

What is written, and where: into the memory of the running Napoleon.exe
process only. Nothing is written to disk and no game file is touched, so the
patch is gone when the game exits and is applied again at the next launch.

  1. Read the bytes at the nine sites (fixbuffer.SITES) at this launch's load
     address and compare them with the stock bytes. Any difference and
     nothing is written.
  2. Allocate one 4 KB executable block in the game with VirtualAllocEx, just
     past the image if possible and otherwise anywhere (jumps wrap in a
     32-bit process), and write the nine checks into it. They are the same
     code fixbuffer.py would put in the file.
  3. Suspend the game's threads, overwrite each site (12 to 19 bytes) with a
     jump to its check padded with NOPs, resume.
  4. Read the sites back and report.

undo() writes the stock bytes back over the nine jumps.

Why memory and not the file: the on-disk patch (fixbuffer.py) hid every
public multiplayer lobby, since a patched exe only sees other patched games.
The same check in memory keeps every lobby visible.

How it is applied: ladder_overlay.py calls apply() once per game process,
automatically, and only when the game's UI table shows no battle on screen
(main menu or lobby); the sites run during battles, and rewriting code a
thread is executing would crash the game. There is no prompt per launch; the
terms the player agrees to on first run (consent.py) state that the game's
memory is written in this one place.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import sys

import fixbuffer

STOCK, PATCHED, UNKNOWN, NOT_RUNNING = "stock", "patched", "unknown", "not running"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

#: VM_OPERATION | VM_READ | VM_WRITE | QUERY_INFORMATION | SUSPEND_RESUME
ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400 | 0x0800
MEM_COMMIT_RESERVE = 0x3000
PAGE_EXECUTE_READWRITE = 0x40
#: The stock image's size; caves are tried just past it first.
IMAGE_SIZE = 0x14EB000

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.FlushInstructionCache.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
k32.CloseHandle.argtypes = [wt.HANDLE]
ntdll.NtSuspendProcess.argtypes = [wt.HANDLE]
ntdll.NtResumeProcess.argtypes = [wt.HANDLE]


def _read(h, addr, n):
    """Read n bytes of the game's memory, or b"" on failure."""
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
        return b""
    return buf.raw[:got.value]


def _write_code(h, addr, data):
    """Write code into the game: make the range writable, write, put the
    protection back, flush the instruction cache."""
    old = wt.DWORD(0)
    if not k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data),
                                PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
        raise OSError(f"VirtualProtectEx at {addr:#x}: error {ctypes.get_last_error()}")
    try:
        n = ctypes.c_size_t(0)
        if (not k32.WriteProcessMemory(h, ctypes.c_void_p(addr), data, len(data), ctypes.byref(n))
                or n.value != len(data)):
            raise OSError(f"WriteProcessMemory at {addr:#x}: error {ctypes.get_last_error()}")
    finally:
        tmp = wt.DWORD(0)
        k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), old.value, ctypes.byref(tmp))
    k32.FlushInstructionCache(h, ctypes.c_void_p(addr), len(data))


def _sites(h, base):
    """[(address, original bytes, state, SITES row)]"""
    out = []
    for row in fixbuffer.SITES:
        rva, hexorig = row[0], row[1]
        orig = bytes.fromhex(hexorig)
        now = _read(h, base + rva, len(orig))
        if now == orig:
            st = STOCK
        elif now[:1] == b"\xE9" and now[5:] == b"\x90" * (len(orig) - 5):
            st = PATCHED
        else:
            st = UNKNOWN
        out.append((base + rva, orig, st, row))
    return out


def _open(pid):
    """(pid, module base, process handle) for the running game; None where unavailable."""
    from lobby_memory import find_game, module_base
    if pid is None:
        pid, _ = find_game()
    if not pid:
        return None, None, None
    base = module_base(pid)
    if not base:
        return pid, None, None
    h = k32.OpenProcess(ACCESS, False, pid)
    return pid, base, h


def state(pid=None):
    """One of stock, patched, unknown or not running."""
    pid, base, h = _open(pid)
    if not h:
        return NOT_RUNNING if not pid else UNKNOWN
    try:
        kinds = {st for _a, _o, st, _r in _sites(h, base)}
    finally:
        k32.CloseHandle(h)
    return kinds.pop() if len(kinds) == 1 else UNKNOWN


def apply(pid=None, log=print):
    """Patch the running game. (ok, message). Never raises."""
    try:
        pid, base, h = _open(pid)
        if not pid:
            return False, "Napoleon is not running"
        if not h:
            return False, f"could not open the game for writing (error {ctypes.get_last_error()})"
        try:
            sites = _sites(h, base)
            kinds = {st for _a, _o, st, _r in sites}
            if kinds == {PATCHED}:
                return True, "already patched in memory"
            if kinds != {STOCK}:
                return False, "not every site holds the stock code; left alone"
            cave = None
            start = (base + IMAGE_SIZE + 0xFFFF) & ~0xFFFF
            for k in range(256):
                cave = k32.VirtualAllocEx(h, ctypes.c_void_p(start + k * 0x10000), 0x1000,
                                          MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE)
                if cave:
                    break
            # Past the image is often taken already; then anywhere will do,
            # since the jumps wrap in a 32-bit process (fixbuffer.rel32).
            if not cave:
                cave = k32.VirtualAllocEx(h, None, 0x1000, MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE)
            if not cave:
                return False, f"could not reserve memory in the game (error {ctypes.get_last_error()})"
            caves, jumps = bytearray(), []
            for addr, orig, _st, (_rva, _hex, breg, creg, preg) in sites:
                code, site = fixbuffer.cave_for(addr, orig, breg, creg, preg, cave + len(caves))
                caves += code
                jumps.append((addr, site))
            ntdll.NtSuspendProcess(h)
            try:
                _write_code(h, cave, bytes(caves))
                for addr, site in jumps:
                    _write_code(h, addr, site)
            finally:
                ntdll.NtResumeProcess(h)
            after = {st for _a, _o, st, _r in _sites(h, base)}
            if after != {PATCHED}:
                return False, f"written, but the sites read back as {sorted(after)}"
            return True, f"patched in memory: 9 sites, checks at {cave:#x}"
        finally:
            k32.CloseHandle(h)
    except Exception as e:                     # noqa: BLE001
        return False, f"failed: {e!r}"


def undo(pid=None):
    """Put the original code back. (ok, message). Never raises."""
    try:
        pid, base, h = _open(pid)
        if not h:
            return False, "Napoleon is not running or cannot be opened"
        try:
            sites = _sites(h, base)
            ntdll.NtSuspendProcess(h)
            try:
                for addr, orig, st, _r in sites:
                    if st == PATCHED:
                        _write_code(h, addr, orig)
            finally:
                ntdll.NtResumeProcess(h)
            after = {st for _a, _o, st, _r in _sites(h, base)}
            return after == {STOCK}, f"sites now {sorted(after)}"
        finally:
            k32.CloseHandle(h)
    except Exception as e:                     # noqa: BLE001
        return False, f"failed: {e!r}"


def main():
    """Command-line entry point: report, --apply or --undo."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true")
    args = ap.parse_args()
    if args.apply:
        ok, msg = apply()
    elif args.undo:
        ok, msg = undo()
    else:
        ok, msg = True, state()
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
