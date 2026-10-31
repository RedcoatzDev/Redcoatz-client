"""Install the post-battle results hook into the game's data folder.

    python postbattle_hook.py            report what is installed
    python postbattle_hook.py --apply    install (or update) the hook
    python postbattle_hook.py --restore  remove it

What is placed where: one file, gamefiles/button_rematch.luac shipped with
this client, is copied to

    <game>/data/ui/battle ui/mp_postbattle_scripts/button_rematch.luac

The engine looks for loose files under data/ before it looks inside packs, so
this shadows the 386-byte original in data.pack. No pack is modified and
there is nothing to back up: --restore deletes our file and the pack's
original is loaded again. If a loose file that is not ours is already there,
it is kept beside ours as button_rematch.luac.redcoatz-backup and put back on
restore. The file is Lua source under a .luac name; Lua accepts either by the
first byte.

What the script does: it runs when the multiplayer post-battle screen is
built. It keeps the Rematch button doing what it always did
(BattleUI.MPVoteRestart). Then, inside pcall so a failure cannot break the
screen, it asks the engine for the battle's results with
BattleUI.PostBattleInfo(), the same table the screen is drawn from, and
writes them as JSON to %APPDATA%/ntw3ladder/postbattle.json, with a short
log in %APPDATA%/ntw3ladder/postbattle.log. It also calls
BattleUI.SaveReplay with a timestamped "redcoatz_..." name so the replay is
saved to the game's replay folder without anyone pressing the button. It
writes nothing else. The client used to read this screen out of the game's
memory, which worked about a third of the time; this is the game's own
numbers every time.

How it is installed: the client (ntw3ladder.py) calls apply() before every
launch without a separate prompt, never while the game is running, and never
fatally. The engine caches scripts for the life of the process, so the file
is not changed under a running game.

A loose script is not a pack, so it is not on the pack list players must
match to play together. To hook one of NTW3's own UI scripts without
replacing it: extract its .luac from the pack, install it beside the hook
under another name, and loadfile() it first; Lua loads bytecode and source
alike.
"""

import argparse
import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: Where the game looks for it, under data/.
REL = os.path.join("ui", "battle ui", "mp_postbattle_scripts",
                   "button_rematch.luac")

#: What we install. Inside the built exe the file travels as data beside the
#: modules (see build.py), so the same relative path works frozen or not.
SOURCE = os.path.join(getattr(sys, "_MEIPASS", HERE), "gamefiles",
                      "button_rematch.luac")

INSTALLED, OUTDATED, FOREIGN, MISSING = ("installed", "outdated", "foreign",
                                          "missing")


def data_dir():
    """The game's data folder, or None."""
    try:
        import gamelaunch
        launcher = gamelaunch.find_launcher()
    except Exception:                                # noqa: BLE001
        launcher = None
    if launcher:
        # ...\data\NTW3\Launcher\NTW3 v9.6.exe -> ...\data
        return os.path.dirname(os.path.dirname(os.path.dirname(launcher)))
    return None


def target(data=None):
    """Full path of the installed hook, or None if the game was not found."""
    data = data or data_dir()
    return os.path.join(data, REL) if data else None


def _sha(path):
    """SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def status(data=None):
    """(state, path). FOREIGN is a loose file there that is not ours."""
    t = target(data)
    if not t:
        return MISSING, None
    if not os.path.isfile(t):
        return MISSING, t
    with open(t, "rb") as f:
        head = f.read(64)
    if b"Redcoatz post-battle hook" not in head:
        return FOREIGN, t
    return (INSTALLED if _sha(t) == _sha(SOURCE) else OUTDATED), t


def game_running():
    """Is Napoleon.exe up? The engine caches a script for the life of the
    process, so changing or removing one under a running game leaves it holding
    code whose files are gone (a dead OK button on the results popup). Nothing
    here touches the folder while it is.
    """
    try:
        from lobby_memory import find_game
        pid, _ = find_game()
        return bool(pid)
    except Exception:                                # noqa: BLE001
        return False


def apply(data=None, log=print):
    """Install or update the hook. Returns True if it is in place."""
    st, t = status(data)
    if not t:
        log("game data folder not found")
        return False
    if st != INSTALLED and game_running():
        log("the game is running; the results hook will be installed at "
            "the next launch instead")
        return False
    if not os.path.isfile(SOURCE):
        log("results hook source missing from this build: %s" % SOURCE)
        return False
    if st == INSTALLED:
        log("already installed: %s" % t)
        return True
    if st == FOREIGN:
        # Somebody else's loose script: kept beside ours, since it is in no
        # pack and deleting it would lose the only copy.
        keep = t + ".redcoatz-backup"
        if not os.path.exists(keep):
            shutil.copy2(t, keep)
            log("kept the existing file as %s" % keep)
    os.makedirs(os.path.dirname(t), exist_ok=True)
    shutil.copy2(SOURCE, t)
    log("%s -> %s" % ("updated" if st == OUTDATED else "installed", t))
    return True


def restore(data=None, log=print):
    """Remove the hook and put back any file that was there before."""
    st, t = status(data)
    if st in (INSTALLED, OUTDATED) and game_running():
        log("the game is running; restore after it closes")
        return False
    if st in (INSTALLED, OUTDATED):
        os.remove(t)
        log("removed %s (the pack's original is used again)" % t)
        keep = t + ".redcoatz-backup"
        if os.path.exists(keep):
            shutil.move(keep, t)
            log("put back the file that was there before: %s" % t)
        return True
    log("nothing of ours is installed")
    return True


def main():
    """Command-line entry point: report, --apply or --restore."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--data", help="the game's data folder, if not found")
    a = ap.parse_args()
    if a.apply:
        return 0 if apply(a.data) else 1
    if a.restore:
        return 0 if restore(a.data) else 1
    st, t = status(a.data)
    print("results hook  %s  %s" % (st, t or "(game not found)"))
    out = os.path.join(os.environ.get("APPDATA", ""), "ntw3ladder",
                       "postbattle.json")
    print("%s  %s" % ("present" if os.path.exists(out) else "no file yet", out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
