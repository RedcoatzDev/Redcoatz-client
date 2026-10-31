"""The NTW3 Ladder client: the one program players run.

    python ntw3ladder.py                  normal use
    python ntw3ladder.py --demo           fake standings, no server, no game
    python ntw3ladder.py --no-upload      panel only, report nothing

In order: sign in through Steam, show the terms on first run, upload the
replays already on disk once, keep uploading finished battles while open, and
show the standings panel beside the game.

What this file itself touches: it writes a log to %APPDATA%\\ntw3ladder\\client.log,
and makes one GET to <server>/healthz as a reachability test. Everything else
(replay folder, Steam files, game files, uploads) is done by the modules it
calls, each of which documents its own paths and requests.

One Tk root is created here and handed to every window as its parent. Tcl
requires the thread that created an interpreter to destroy it, and building
and tearing down several roots in sequence aborted the process with
"Tcl_AsyncDelete: async handler deleted by the wrong thread".
"""

import argparse
import os
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# Diagnostics first. A PyInstaller --windowed build has no console, so an
# exception anywhere, including in the imports below, would otherwise produce
# no window, no message and no exit code. Output goes to a log file and any
# unhandled failure gets a message box, both set up before the first project
# import.

LOG_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder")
LOG_PATH = os.path.join(LOG_DIR, "client.log")


class _Tee:
    """Write to the log file and to the console, if there is one."""

    def __init__(self, f, also=None):
        self._f, self._also = f, also

    def write(self, text):
        n = self._f.write(text)
        if self._also is not None:
            try:
                self._also.write(text)
            except Exception:
                # A console that has gone away must not take the log with it.
                self._also = None
        return n

    def flush(self):
        self._f.flush()
        if self._also is not None:
            try:
                self._also.flush()
            except Exception:
                self._also = None

    def isatty(self):
        return False


def _capture_output():
    """Send stdout and stderr to %APPDATA%\\ntw3ladder\\client.log and to the console.

    Always, not only when there is no console: a run from a terminal is
    otherwise unrecorded once the terminal is gone.
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        f = open(LOG_PATH, "a", encoding="utf-8", buffering=1,
                 errors="replace")
    except OSError:
        # Nowhere to write. Leave the streams alone rather than swallowing
        # the output into os.devnull, which would take the console with it.
        return None
    sys.stdout = _Tee(f, sys.stdout)
    sys.stderr = _Tee(f, sys.stderr)
    return LOG_PATH


_LOGFILE = _capture_output()

# The version is logged before any other import: version.py is a bare constant
# so this works even for a build that fails before its own modules load.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from version import VERSION as _V
except Exception:
    _V = "unknown"

print(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')}  "
      f"ntw3ladder v{_V}  pid {os.getpid()}")
print(f"    frozen={getattr(sys, 'frozen', False)} python={sys.version.split()[0]}")
print(f"    exe={sys.executable}")
print(f"    cwd={os.getcwd()}")
# The file's own timestamp and size tie a report to a specific build even if
# the file was renamed.
try:
    _p = sys.executable if getattr(sys, "frozen", False) else __file__
    _st = os.stat(_p)
    print(f"    build={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_st.st_mtime))}"
          f" {_st.st_size} bytes")
except Exception:
    pass


def _message_box(title, text):
    """Show a native Windows message box via ctypes, so it works even when tkinter fails to load."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _die(stage, exc):
    """Log a failure, show it, and exit. Never returns."""
    detail = "".join(traceback.format_exception(type(exc), exc,
                                                exc.__traceback__))
    print(f"FATAL during {stage}:\n{detail}")
    _message_box(
        "NTW3 Ladder could not start",
        f"Something went wrong while {stage}.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        f"A full report was saved to:\n{LOG_PATH}\n\n"
        "Please send that file to the Redcoatz Discord.")
    sys.exit(3)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tkinter as tk

    import nettrust  # noqa: F401  TLS via the OS store, before any request
    import consent
    import firstsync
    import uploader
    import gamelaunch
    import update
    import steamauth
    from ladder_overlay import Overlay, DEFAULT_SERVER
except Exception as _e:                      # noqa: BLE001 - report anything
    _die("loading the client", _e)


def _probe(label, fn):
    """Log one fact about this machine; a probe that fails logs the error instead. Never raises."""
    try:
        print(f"    {label:22} {fn()}")
    except Exception as e:                   # noqa: BLE001
        print(f"    {label:22} FAILED: {type(e).__name__}: {e}")


def report_environment(server):
    """Write the machine's state to the local log before the first window opens.

    Records paths, counts and yes/no answers only: OS version, log and
    consent paths, Steam library roots and whether loginusers.vdf exists,
    the replay folder and how many .replay files it holds. It makes one GET
    to <server>/healthz to test reachability. Nothing here is sent anywhere;
    it goes into client.log so a player's bug report can be read.
    """
    import platform

    print("  --- environment ---")
    _probe("os", lambda: f"{platform.system()} {platform.release()} "
                         f"({platform.version()}) {platform.machine()}")
    _probe("log file", lambda: LOG_PATH)
    _probe("appdata", lambda: os.environ.get("APPDATA", "(unset)"))

    print("  --- network ---")
    _probe("server", lambda: server)
    _probe("os cert trust", lambda: nettrust.OS_TRUST)

    def reach():
        import time as _t
        import urllib.request
        t0 = _t.time()
        req = urllib.request.Request(server.rstrip("/") + "/healthz",
                                     headers={"User-Agent": "diag"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"HTTP {r.status} in {int((_t.time()-t0)*1000)}ms"
    _probe("reach /healthz", reach)

    print("  --- identity and state ---")
    _probe("consent file", lambda: consent.CONSENT_FILE)
    _probe("already agreed", lambda: consent.has_agreed())
    _probe("terms version", lambda: consent.TERMS_VERSION)
    _probe("first sync pending", lambda: firstsync.pending())

    print("  --- steam and the game ---")
    _probe("steam libraries", lambda: gamelaunch._steam_roots() or "none found")
    _probe("loginusers.vdf",
           lambda: next((p for p in (
               os.path.join(r, "config", "loginusers.vdf")
               for r in gamelaunch._steam_roots())
               if os.path.isfile(p)), "NOT FOUND"))
    _probe("ntw3 launcher", lambda: gamelaunch.find_launcher() or "NOT FOUND")
    _probe("game running", lambda: gamelaunch.already_running())

    print("  --- replays ---")
    _probe("replay folder", lambda: uploader.REPLAY_DIR)
    _probe("folder exists", lambda: os.path.isdir(uploader.REPLAY_DIR))
    _probe("replay count", lambda: len(
        [f for f in os.listdir(uploader.REPLAY_DIR)
         if f.lower().endswith(".replay")])
        if os.path.isdir(uploader.REPLAY_DIR) else "n/a")
    _probe("chatvalentin dir", lambda: (
        f"{uploader.CV_DIR} exists={os.path.isdir(uploader.CV_DIR)}"))


def report_display(root):
    """Log screen size and DPI, so an off-screen or badly scaled dialog can be diagnosed."""
    print("  --- display ---")
    _probe("screen", lambda: f"{root.winfo_screenwidth()}x"
                             f"{root.winfo_screenheight()} "
                             f"dpi={root.winfo_fpixels('1i'):.0f}")
    _probe("tk version", lambda: root.tk.call("info", "patchlevel"))


def step(what):
    """Mark progress in the log.

    A log that ends on a step with no traceback means the process was
    stopped from outside (antivirus, for example); a traceback means the
    fault is ours. The two need different fixes.
    """
    print(f"  [{time.strftime('%H:%M:%S')}] {what}", flush=True)


class _FixesRefused(Exception):
    """Leaves the crash-fix dialog early once the player has said no."""


def main():
    """Parse the command line and run the client start-up sequence. Returns the exit code."""
    ap = argparse.ArgumentParser(description="NTW3 Ladder client")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--demo", action="store_true",
                    help="fake standings; contacts no server and reads no game")
    ap.add_argument("--no-upload", action="store_true",
                    help="show the panel but do not report battles")
    ap.add_argument("--scale", type=float, default=None,
                    help="override DPI scale, e.g. 1.5")
    ap.add_argument("--no-auto-hide", action="store_true",
                    help="stay open during battles instead of collapsing")
    ap.add_argument("--no-game", action="store_true",
                    help="do not start NTW3; just show the panel")
    ap.add_argument("--game-path", default=None,
                    help="full path to the NTW3 launcher, if it is not found")
    ap.add_argument("--no-fix23e", action="store_true",
                    help="do not offer to repair the 23e bug in variants94b.pack")
    ap.add_argument("--no-results-hook", action="store_true",
                    help="do not install the script that hands the client the "
                         "game's own battle results and saves every replay")
    ap.add_argument("--no-fixmiles", action="store_true",
                    help="do not offer to repair the sound crash in mss32.dll")
    ap.add_argument("--no-fixbuffer", action="store_true",
                    help="do not offer to bound the 100-unit targeting buffer "
                         "in Napoleon.exe (the big-battle crash)")
    ap.add_argument("--game-settings", action="store_true",
                    help="open the game diagnostics dialog and exit")
    ap.add_argument("--allow-multiple", action="store_true",
                    help="start even if a copy is already running. For "
                         "development; two copies upload every battle twice")
    ap.add_argument("--sign-out", action="store_true",
                    help="disconnect this computer from your Steam account on "
                         "Redcoatz: the key is revoked and forgotten, and the "
                         "next start asks you to sign in again")
    ap.add_argument("--make-shortcut", action="store_true",
                    help="put a Desktop shortcut for this client, then exit")
    ap.add_argument("--after-update", type=int, default=None, metavar="PID",
                    help=argparse.SUPPRESS)
    ap.add_argument("--self-update", action="store_true",
                    help="download and install the latest version, then restart")
    args = ap.parse_args()

    # Started by the old copy after a one-click update (selfupdate.py): wait
    # for it to exit before taking the single-instance lock, then remove what
    # the swap left behind.
    import selfupdate
    if args.after_update:
        step(f"started by an update; waiting for pid {args.after_update} to exit")
        selfupdate.wait_for_exit(args.after_update)
    selfupdate.cleanup(step)
    if args.self_update:
        # The same thing as clicking the version link, from the command line.
        # Exits through the restart on success; on failure the window says
        # why and closing it ends this run. tk must stay the module-level
        # import: importing it here would make it local to all of main.
        if not selfupdate.can_self_update():
            print("--self-update only works from the built Redcoatz.exe")
            return 2
        root = tk.Tk()
        root.withdraw()
        w = selfupdate.run_gui(root, on_restart=lambda: os._exit(0), log=step)
        w.protocol("WM_DELETE_WINDOW", root.destroy)
        w.bind("<Destroy>", lambda e: root.after_idle(root.destroy) if e.widget is w else None)
        root.mainloop()
        return 1

    # One copy at a time, before anything reads a folder or sends a request.
    # --demo is exempt: it contacts no server and reads no game.
    if not args.demo and not args.allow_multiple:
        # Imported here so that an import failure is caught by the diagnostics.
        import singleton
        if not singleton.claim():
            step("another copy is already running; not starting a second")
            singleton.warn_gui()
            return 0

    if args.make_shortcut:
        made, msg = gamelaunch.make_desktop_shortcut()
        print(f"shortcut: {msg}" if made else f"could not make a shortcut: {msg}")
        return 0 if made else 1

    # Disconnect from Steam, from the command line. No window, no game.
    if args.sign_out:
        return 0 if steamauth.sign_out(args.server, log=step) else 1

    # A dialog and nothing else, so a player can change these while their
    # game or another copy of the client is open.
    if args.game_settings:
        import gameprefs
        gameprefs.dialog()
        return 0

    # Ask Windows for real pixels rather than a blurry upscaled window.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    # Write down what this machine looks like before anything can go wrong.
    report_environment(args.server)

    # The one and only interpreter. Hidden until the panel is built, so the
    # terms and the first upload are not shown behind an empty grey square.
    step("creating the window")
    root = tk.Tk()
    root.withdraw()
    report_display(root)

    stop = threading.Event()

    # The newest version the server advertises, if newer than this one. Shown
    # in the panel's title bar rather than in a dialog.
    update_to = None
    player_name, player_sid = None, None

    if not args.demo:
        # Before the terms, any upload or the game: a client the ladder has
        # retired must not reach any of it, so a known-bad version cannot
        # write to other people's ratings.
        step(f"checking the client version against {args.server}")
        allowed, info = update.check(args.server)
        step(f"version check: allowed={allowed} info={info}")
        if not allowed:
            update.block_gui(info, parent=root)
            root.destroy()
            return 2

        # A newer build exists that this player is not required to take.
        if update.outdated_but_usable(info):
            update_to = info.get("latest")
            step(f"an update is available: v{update_to}")

        # Never fatal: a player who cannot be identified should still see the
        # terms and the panel.
        step("working out who you are (this reads game memory)")
        try:
            name, sid = uploader.detect_player()
        except Exception as e:               # noqa: BLE001
            print(f"detect_player failed, continuing without a name: {e!r}")
            name, sid = None, None
        step(f"player: {name!r} steam={sid}")
        # Kept under names of their own: the crash-fix step below reuses
        # `name` as its loop variable.
        player_name, player_sid = name, sid

        # Banned for cheating (an army far over the lobby's gold limit, seen
        # in the replays). Checked before the terms and before anything is
        # uploaded. Fails open, like the version check.
        if name or sid:
            banned, binfo = update.check_banned(args.server, name, sid)
            step(f"ban check: banned={banned}")
            if banned:
                update.banned_gui(binfo, parent=root)
                root.destroy()
                return 3

        # Sign in through Steam first; nothing happens without it. The browser
        # does the work and the key comes back to a listener on localhost
        # (steamauth.py). Drawn in the panel's own window (sign_in_screen);
        # only Quit gets here without a key.
        step("signing in through Steam")
        try:
            from ladder_overlay import sign_in_screen
            verified = sign_in_screen(root, args.server, sid, log=step,
                                      scale=args.scale)
        except Exception as e:               # noqa: BLE001
            print(f"steam sign-in failed: {e!r}")
            verified = None
        step(f"steam sign-in: verified={verified}")
        if not verified:
            step("not signed in; exiting")
            root.destroy()
            return 1
        steamauth.remember_name(sid, name)

        step("showing the terms")
        if not consent.require_consent_gui(args.server, player=name,
                                           steam_id=sid, parent=root):
            step("terms declined or dismissed; exiting")
            root.destroy()
            # Say so: a window that appears and vanishes in silence reads as
            # a crash. Declining has to look like a choice.
            _message_box(
                "NTW3 Ladder",
                "You did not accept the terms, so the client has closed.\n\n"
                "Nothing was uploaded.\n\n"
                "Run it again if you change your mind.")
            return 1
        step("terms accepted")
        # The terms window posts the agreement itself; this retries one that
        # was recorded locally but refused or never reached the server.
        step(f"consent sent: {consent.sync(args.server, name, sid)}")

        if name and not args.no_upload:
            # Send what is already on disk, once, before the panel opens, in
            # its own progress window.
            if firstsync.pending():
                step("first upload sweep")
                firstsync.run_gui(args.server, name, parent=root)
                step("first upload finished")

            # Then keep going for the whole session. A daemon thread so the
            # process still exits when the panel closes.
            threading.Thread(
                target=uploader.watch_forever,
                args=(args.server, name),
                kwargs={"stop": stop},
                daemon=True,
            ).start()
            step("upload watcher started")
        elif not name and not args.no_upload:
            # Not fatal. The panel is still useful.
            step("no player name; not uploading this session")

    # Offer to repair the known crash causes in the player's own install, in
    # one step so the player is asked once. Before start_game, because the
    # running game holds the pack and the DLL open. Never fatal.
    if not args.demo and not args.no_game:
        step("checking for known crash causes")
        # (module name, description). The description is built only when a
        # fix is needed, because the 23e wording counts the entries it will
        # repair and that count comes from status().
        wanted = []
        if not args.no_fix23e:
            wanted.append(("fix23e", lambda st: (
                "The 23e bug: the 23e leger's flag bearer asks for textures "
                "the game has not loaded yet, which can crash the battle. "
                "Repairing %d unit entr%s rewrites a few hundred bytes of "
                "texture paths."
                % (st.get("old", 0), "y" if st.get("old", 0) == 1 else "ies")
            )))
        if not args.no_fixmiles:
            wanted.append(("fixmiles", lambda st: (
                "The sound crash: when a sound finishes at the wrong moment "
                "the sound library keeps reading past the end of it, and the "
                "game closes with no warning and no error message. The fix "
                "changes three bytes in mss32.dll."
            )))
        # The on-disk Napoleon.exe patch is withdrawn: a patched exe only sees
        # other patched games in the public lobby list. It is never offered,
        # and where an earlier version applied it, the original exe is put
        # back from the backup the patcher kept.
        try:
            import fixbuffer
            fb = fixbuffer.status()
            if fb["state"] == fixbuffer.FIXED:
                step("fixbuffer: Napoleon.exe is patched; restoring the original")
                from tkinter import messagebox
                try:
                    from lobby_memory import find_game
                except Exception:              # noqa: BLE001
                    find_game = None
                while find_game is not None and find_game()[0]:
                    if not messagebox.askretrycancel(
                            "NTW3 Ladder",
                            "Redcoatz 0.6.7 changed Napoleon.exe to fix an "
                            "artillery crash, and that change hides the "
                            "public multiplayer games from you. Redcoatz needs "
                            "to put the original back.\n\n"
                            "Close Napoleon, then choose Retry.",
                            parent=root):
                        break
                ok, msg = fixbuffer.restore()
                step(f"  fixbuffer restore: ok={ok} {msg}")
                if ok:
                    messagebox.showinfo(
                        "NTW3 Ladder",
                        "Napoleon.exe is back to the original, so you will "
                        "see every multiplayer game again.\n\n"
                        "The artillery crash is still fixed: Redcoatz now "
                        "applies the fix to the running game each time, "
                        "without changing the file.",
                        parent=root)
                else:
                    messagebox.showwarning(
                        "NTW3 Ladder",
                        "Redcoatz could not put the original Napoleon.exe "
                        "back: " + msg + "\n\n"
                        "In Steam, right-click Napoleon: Total War, "
                        "Properties, Installed Files, Verify integrity of "
                        "game files. That restores it too.",
                        parent=root)
        except Exception as e:                 # noqa: BLE001
            step(f"fixbuffer restore check failed: {e!r}")

        todo = []
        for name, describe in wanted:
            try:
                mod = __import__(name)
                st = mod.status()
                step(f"  {name}: {st['state']} -- {st['detail']}")
                if st["state"] == mod.STOCK:
                    todo.append((mod, describe(st)))
            except Exception as e:            # noqa: BLE001
                step(f"  {name} check failed, continuing without it: {e!r}")

        # Required, not offered: a patched and an unpatched game desync at
        # the moment the bug would have fired, so the ladder runs only on a
        # fixed install. Declining, or a failed repair, closes the client
        # the way declining the terms does.
        #
        # Only a file in its known stock state is held to this. A file the
        # repair does not recognise cannot be fixed by this tool, so that
        # player is logged above and passes.
        if todo:
            refused = None
            try:
                from tkinter import messagebox
                # A running game holds these files open and Windows refuses
                # to overwrite a file in use, so ask the player to close it
                # first and wait as long as they like.
                try:
                    from lobby_memory import find_game
                except Exception:              # noqa: BLE001
                    find_game = None
                while find_game is not None and find_game()[0]:
                    step("crash fixes: Napoleon is running; asking to close it")
                    if not messagebox.askretrycancel(
                            "NTW3 Ladder",
                            "Close Napoleon so Redcoatz can apply the crash "
                            "fixes.\n\n"
                            "The game keeps its files open while it runs, so "
                            "they cannot be repaired until it is closed. "
                            "Redcoatz starts it again for you afterwards.\n\n"
                            "Close the game, then choose Retry. Cancel closes "
                            "Redcoatz.",
                            parent=root):
                        refused = (
                            "Redcoatz only runs on an install with the known "
                            "crash fixes applied, and they cannot be applied "
                            "while Napoleon is running, so it has closed.\n\n"
                            "Nothing was changed and nothing was uploaded.\n\n"
                            "Close the game, then run Redcoatz again.")
                        break
                if refused:
                    raise _FixesRefused()
                n = len(todo)
                body = "\n\n".join("• " + d for _m, d in todo)
                if messagebox.askyesno(
                        "NTW3 Ladder",
                        ("Found %d known cause%s of crashes in your install:"
                         "\n\n%s\n\n"
                         "Redcoatz needs %s fixed before it can run: one "
                         "player without the fixes can crash or desync a "
                         "whole lobby. The original files are backed up "
                         "first, and Steam's Verify integrity of game files "
                         "undoes any of it at any time.\n\n"
                         "Fix %s now? Choosing No closes Redcoatz."
                         % (n, "" if n == 1 else "s", body,
                            "it" if n == 1 else "them",
                            "it" if n == 1 else "them")),
                        parent=root):
                    said, failed = [], []
                    for mod, _d in todo:
                        ok, msg = mod.apply(log=step)
                        step(f"  {mod.__name__}: ok={ok} {msg}")
                        said.append(msg)
                        if not ok:
                            failed.append(msg)
                    if failed:
                        refused = (
                            "Redcoatz could not apply every fix, so it has "
                            "closed:\n\n" + "\n\n".join(failed) + "\n\n"
                            "Close Napoleon if it is running, and if that is "
                            "not it, run Redcoatz as administrator. Nothing "
                            "was uploaded.")
                    else:
                        messagebox.showinfo("NTW3 Ladder", "\n\n".join(said),
                                            parent=root)
                else:
                    refused = (
                        "Redcoatz only runs on an install with the known "
                        "crash fixes applied, so it has closed.\n\n"
                        "Nothing was changed and nothing was uploaded.\n\n"
                        "Run it again and choose Yes when you are ready.")
            except _FixesRefused:
                pass
            except Exception as e:            # noqa: BLE001
                # The dialog itself failed. Not the player's choice, so not a
                # reason to lock them out: log it and carry on.
                step(f"crash fixes failed, continuing without them: {e!r}")
            if refused:
                step("crash fixes declined or failed; exiting")
                root.destroy()
                _message_box("NTW3 Ladder", refused)
                return 1

    # Tell the ladder this client is here and which crash fixes are on, for
    # the red coat beside this player's name in other players' lobby panels.
    # After the crash-fix step so a "yes" just given is what gets reported.
    # Background and never fatal; see clientstatus.py.
    if not args.demo and player_name:
        try:
            import clientstatus
            clientstatus.send(args.server, player_name, player_sid, log=step)
        except Exception as e:                # noqa: BLE001
            step(f"client status skipped: {e!r}")

    # The game's own diagnostic flags, written into its preferences file just
    # before it starts. The game rewrites that file on exit and drops anything
    # it does not save itself, so this is re-applied every launch. Off by
    # default; see gameprefs.py.
    if not args.demo and not args.no_game:
        try:
            import gameprefs
            ok, msg = gameprefs.apply_to_game(log=step)
            step(f"game settings: {msg}")
        except Exception as e:                # noqa: BLE001
            # Never fatal. These are diagnostics.
            step(f"game settings skipped: {e!r}")

    # The results hook: a script the game runs on its post-battle screen that
    # writes the battle's results table and saves the replay. Kept current
    # before every launch, never while the game is running, never fatal. See
    # client/gamefiles and postbattle_hook.py.
    if not args.demo and not args.no_game and not args.no_results_hook:
        try:
            import postbattle_hook
            ok = postbattle_hook.apply(log=step)
            step(f"results hook: {'in place' if ok else 'not installed'}")
        except Exception as e:                # noqa: BLE001
            step(f"results hook skipped: {e!r}")

    # Start the game last, once the terms are settled and the first upload is
    # done, so the launcher does not cover the windows the player must answer.
    if not args.demo and not args.no_game:
        step("starting NTW3")
        started, msg = gamelaunch.start_game(args.game_path)
        step(f"game: {msg}")
        # Not fatal either way: a player who already had NTW3 open still gets
        # a working panel.

    step("building the panel")
    Overlay(root, args.server, demo=args.demo, scale=args.scale,
            watch_game=not args.no_auto_hide, update_to=update_to)
    root.deiconify()
    step("panel is up; running")
    try:
        root.mainloop()
    finally:
        stop.set()
    step("panel closed; exiting normally")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:                   # noqa: BLE001 - report anything
        # The last line of defence: without it a windowed build closes with
        # no window, no message and nothing written down.
        _die("running", e)
