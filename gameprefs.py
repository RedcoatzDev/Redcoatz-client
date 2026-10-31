"""Set two of the game's own diagnostic flags from the client before it starts.

    python gameprefs.py                  show what is set and what the game has
    python gameprefs.py --set sync=on
    python gameprefs.py --set logs=on
    python gameprefs.py --apply          push our settings into the game's file

Files touched:

    %APPDATA%/ntw3ladder/gameprefs.json
        Ours: the desired values, written by save().

    %APPDATA%/The Creative Assembly/Napoleon/scripts/preferences.script.txt
        The game's own settings file (UTF-16, one setting per line). Read by
        game_values(); rewritten by apply_to_game(), which the client runs
        automatically before every launch. Only the lines for the settings
        below are changed or appended; every other line is written back as
        read. The first time it is rewritten the original is copied to
        preferences.script.txt.redcoatz-backup beside it and kept.

Nothing else is read or written. With the default values the only changes
are to append the two settings as false if the game's file lacks them, and
to put mp_desync_kick_enabled back to true if an old client set it to false.

Napoleon.exe reads that file at startup and rewrites it at exit, dropping any
setting it does not save itself, which includes these. So the desired values
live in our file and are pushed into the game's file just before each launch.

The settings, neither of which is in the game's options screen. Each is one
byte in Napoleon.exe, which is how the effect was verified rather than
assumed:

    mp_network_sync_logging   Napoleon.exe+0x10a12c6   log the battle checksum every tick
    output_sync_logs          Napoleon.exe+0x10a7b33   write those logs to disk

Both are off by default: one session with them on produced 22,296 files and
47 MB, nothing cleans them up, and they cost frames. A third setting,
mp_desync_kick_enabled, was once offered here and withdrawn as confusing; it
is now pinned back to the game's default on every launch (RETIRED) so nobody
is left where the old checkbox put them.
"""

import argparse
import io
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))

#: The game's own settings file. UTF-16 with a BOM, one setting per line.
GAME_PREFS = os.path.join(APPDATA, "The Creative Assembly", "Napoleon",
                          "scripts", "preferences.script.txt")

#: Ours. The source of truth, because the game's copy is transient.
OUR_PREFS = os.path.join(APPDATA, "ntw3ladder", "gameprefs.json")

#: key -> (setting name, default, what it does, the warning if any)
SETTINGS = {
    "sync": (
        "mp_network_sync_logging", False,
        "Log the battle checksum every tick",
        "One file per tick. A session made 22,000 files and 47 MB, and "
        "nothing deletes them.",
    ),
    "logs": (
        "output_sync_logs", False,
        "Write those logs to disk",
        "Needed for the setting above to produce anything.",
    ),
}

#: Settings this client used to offer and now puts back to the game's
#: default: a client that ignores desyncs keeps playing a simulation nobody
#: else shares.
RETIRED = {
    "mp_desync_kick_enabled": "true",
}


def _read_json(path):
    """Parse a JSON file, or {} if it is missing or malformed."""
    try:
        import json
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load():
    """Our desired value for each key, falling back to the game's default."""
    saved = _read_json(OUR_PREFS)
    return {k: bool(saved.get(k, d[1])) for k, d in SETTINGS.items()}


def save(values):
    """Store the desired values. Does not touch the game's file."""
    import json
    os.makedirs(os.path.dirname(OUR_PREFS), exist_ok=True)
    keep = {k: bool(v) for k, v in values.items() if k in SETTINGS}
    with io.open(OUR_PREFS, "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=2)
    return keep


def _load_game_file():
    """The game's settings file as a list of lines, or None if unreadable.

    UTF-16 with a BOM, and written back the same way: the game will not start
    if this file is malformed.
    """
    try:
        with io.open(GAME_PREFS, encoding="utf-16") as f:
            return f.read().splitlines()
    except (OSError, ValueError, UnicodeError):
        return None


def game_values():
    """What the game's file currently says, for the keys we know about."""
    lines = _load_game_file()
    if lines is None:
        return {}
    out = {}
    for key, (name, _d, _l, _w) in SETTINGS.items():
        for line in lines:
            s = line.strip()
            if s.startswith(name + " "):
                out[key] = s.split(";")[0].split()[-1].lower() == "true"
                break
    return out


def apply_to_game(values=None, log=print):
    """Write our settings into the game's file. Call before the game starts.

    Returns (ok, message). Declines rather than guessing when the file is not
    where it should be: a missing file here means the game has never run, and
    creating one from nothing risks a format the game will not read.
    """
    values = load() if values is None else values
    lines = _load_game_file()
    if lines is None:
        return False, "the game's preferences file is not readable; nothing set"

    # One backup, kept forever: the file as the game itself wrote it.
    bak = GAME_PREFS + ".redcoatz-backup"
    if not os.path.exists(bak):
        try:
            shutil.copy2(GAME_PREFS, bak)
            log(f"  backed up -> {os.path.basename(bak)}")
        except OSError:
            pass                      # a missing backup must not stop the fix

    changed = []
    for key, (name, _d, label, _w) in SETTINGS.items():
        want = "true" if values.get(key) else "false"
        line = f"{name} {want};   # {name} <bool>, {label} #"
        for i, existing in enumerate(lines):
            if existing.strip().startswith(name + " "):
                if existing.strip() != line:
                    lines[i] = line
                    changed.append(f"{name}={want}")
                break
        else:
            lines.append(line)
            changed.append(f"{name}={want}")
    # A retired setting is only ever put back to the default, never added.
    for name, want in RETIRED.items():
        for i, existing in enumerate(lines):
            if existing.strip().startswith(name + " "):
                if existing.strip().split(";")[0].split()[-1].lower() != want:
                    lines[i] = f"{name} {want};   # {name} <bool> #"
                    changed.append(f"{name}={want} (restored)")
                break

    if not changed:
        return True, "the game's settings already match"
    try:
        # newline="" or Python also translates each \n into os.linesep,
        # giving \r\r\n and a file that doubles in length on every run.
        with io.open(GAME_PREFS, "w", encoding="utf-16", newline="") as f:
            f.write("\r\n".join(lines) + "\r\n")
    except OSError as e:
        return False, f"could not write the game's preferences: {e}"
    for c in changed:
        log(f"  set {c}")
    return True, f"set {len(changed)} game setting(s)"


def dialog(parent=None):
    """A checkbox per setting. Saves on OK and applies before the next launch.

    Plain by design: every box makes the game slower or noisier on disk, and
    the wording says so.
    """
    import tkinter as tk
    from tkinter import messagebox

    owns = parent is None
    root = tk.Tk() if owns else tk.Toplevel(parent)
    root.title("NTW3 diagnostics")
    root.resizable(False, False)
    frame = tk.Frame(root, padx=16, pady=14)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, justify="left", wraplength=430, anchor="w",
             text="Settings inside Napoleon.exe that the game's own options "
                  "screen does not show. They are applied every time this "
                  "client starts the game, because the game forgets them "
                  "when it closes.").pack(fill="x", pady=(0, 12))

    current = load()
    vars_ = {}
    for key, (name, _d, label, warn) in SETTINGS.items():
        vars_[key] = tk.BooleanVar(value=current[key])
        box = tk.Frame(frame)
        box.pack(fill="x", pady=(0, 10))
        tk.Checkbutton(box, variable=vars_[key], text=label,
                       anchor="w").pack(fill="x")
        tk.Label(box, text=warn, justify="left", wraplength=400, anchor="w",
                 fg="#777").pack(fill="x", padx=(22, 0))
        tk.Label(box, text=name, justify="left", anchor="w",
                 fg="#aaa").pack(fill="x", padx=(22, 0))

    where = game_values()
    if where:
        state = ", ".join(f"{SETTINGS[k][0]}={'true' if v else 'false'}"
                          for k, v in where.items())
        tk.Label(frame, text=f"in the game's file now: {state}",
                 wraplength=430, justify="left", anchor="w",
                 fg="#777").pack(fill="x", pady=(0, 10))

    row = tk.Frame(frame)
    row.pack(fill="x")

    def ok():
        save({k: v.get() for k, v in vars_.items()})
        ok_, msg = apply_to_game(log=lambda *_: None)
        messagebox.showinfo("NTW3 Ladder",
                            msg if ok_ else f"Saved, but: {msg}\n\nThe "
                            "settings will be applied the next time this "
                            "client starts the game.",
                            parent=root)
        root.destroy()

    tk.Button(row, text="Cancel", width=10,
              command=root.destroy).pack(side="right")
    tk.Button(row, text="Save", width=10, command=ok).pack(side="right",
                                                           padx=(0, 8))
    if owns:
        root.mainloop()


def main():
    """Command-line entry point: --set, --apply, --gui, or a report."""
    ap = argparse.ArgumentParser(description="game diagnostic settings")
    ap.add_argument("--set", action="append", metavar="KEY=on|off",
                    help=f"one of {', '.join(SETTINGS)}")
    ap.add_argument("--apply", action="store_true",
                    help="push our settings into the game's file now")
    ap.add_argument("--gui", action="store_true", help="open the dialog")
    args = ap.parse_args()

    if args.set:
        values = load()
        for pair in args.set:
            k, _, v = pair.partition("=")
            if k not in SETTINGS:
                raise SystemExit(f"unknown setting {k!r}; "
                                 f"try one of {', '.join(SETTINGS)}")
            values[k] = v.strip().lower() in ("on", "true", "1", "yes")
        save(values)
    if args.gui:
        dialog()
        return 0
    if args.apply:
        ok, msg = apply_to_game()
        print(f"  {'ok' if ok else 'FAILED'}: {msg}")

    print(f"\n  our settings   {OUR_PREFS}")
    for k, v in load().items():
        print(f"     {SETTINGS[k][0]:<26} {'on' if v else 'off'}")
    got = game_values()
    print(f"\n  the game's file {GAME_PREFS}")
    if not got:
        print("     none of these are in it")
    for k, v in got.items():
        print(f"     {SETTINGS[k][0]:<26} {'true' if v else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
