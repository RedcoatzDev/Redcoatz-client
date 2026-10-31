"""Refuse to start when this client is already running.

Two copies both watch the replay folder: every replay is uploaded twice, the
second is shown as rejected, client.log is garbled by two writers, and
uploaded.json is overwritten by whichever copy saves last, so the client's own
record disagrees with the server's.

The lock is a Windows named mutex, which the kernel destroys when the process
ends however it ends, so there is no stale lock to leave a player locked out
after a crash. Not a process-name check: a PyInstaller one-file launch is
always two processes with the same name. Not a socket: on machines whose
antivirus already distrusts the binary, that invites a firewall prompt.

"Local\\" scopes the mutex per login session, so two people on one PC through
fast user switching each get a client. Nothing is read or written to disk or
the network here.
"""

import sys

#: Anything unique and stable. The name is the lock.
NAME = r"Local\ntw3ladder-client-singleton"

_ERROR_ALREADY_EXISTS = 183

#: Held for the life of the process. Kept in a module global so it is not
#: garbage collected: closing the handle releases the lock.
_handle = None


def claim():
    """True if we are the only copy. False if another is already running.

    True on any platform without this API, and on any unexpected failure: a
    guard that cannot be evaluated must not stop a player from using the
    client. Being wrong that way costs a duplicate upload the server
    tolerates; being wrong the other way costs somebody their client.
    """
    global _handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        _handle = kernel32.CreateMutexW(None, False, NAME)
        err = ctypes.get_last_error()
        if not _handle:
            return True                      # could not ask; let them through
        return err != _ERROR_ALREADY_EXISTS
    except Exception:
        return True


def release():
    """Let go of the lock before this process exits.

    For the self-update (selfupdate.py): the old copy starts the new one and
    then exits, and without this the new one could see the old one's lock
    for the moment the two overlap. Harmless when nothing is held.
    """
    global _handle
    if _handle and sys.platform == "win32":
        try:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(_handle)
        except Exception:                    # noqa: BLE001
            pass
    _handle = None


def warn_gui():
    """Say that a copy is already running, in a message box. Blocks until dismissed.

    A silent exit would be worse than the bug: the panel usually sits behind
    the game, so somebody who sees nothing happen will double-click again.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "NTW3 Ladder is already running",
            "The NTW3 Ladder client is already open.\n\n"
            "Look for the panel over the game -- it hides itself during a "
            "battle and comes back in the lobby.\n\n"
            "Running two copies uploads every battle twice, so this one will "
            "close.")
        root.destroy()
    except Exception:
        print("the NTW3 Ladder client is already running")
