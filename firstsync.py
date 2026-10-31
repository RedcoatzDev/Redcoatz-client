"""The first upload sweep: send the replays already on disk, once, with a progress window.

Steady-state uploading only wants battles fought from now on. A new player
arrives with a folder of past battles, and a ladder that ignored them would
start everybody at zero regardless of history. The terms shown before this
runs say plainly that .replay files are uploaded.

The sweep is done once per machine, recorded by the marker file
%APPDATA%\\ntw3ladder\\firstsync.done (the only thing this module writes).
Uploads are deduplicated by file hash on both sides, so a repeated sweep would
re-send nothing, but it would re-read every file for no result. The uploading
itself is uploader.Uploader; see that module for what is read and sent.
"""

import os
import queue
import threading

STATE_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ntw3ladder")
MARKER = os.path.join(STATE_DIR, "firstsync.done")


def pending():
    """True if the initial sweep has not been done on this machine."""
    return not os.path.exists(MARKER)


def mark_done():
    """Write the marker file. If that fails the sweep runs again next launch and re-sends nothing."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(MARKER, "w", encoding="utf-8") as f:
            f.write("done\n")
    except OSError:
        pass


def _sweep(up, out, stop):
    """Upload everything on disk, reporting progress through a queue.

    Runs off the UI thread: hashing and sending a folder of replays would
    otherwise freeze the window for the whole sweep.
    """
    try:
        paths = up.scan(backfill=True)
        out.put(("total", len(paths)))
        done = 0
        for p in paths:
            if stop.is_set():
                out.put(("cancelled", done))
                return
            name = os.path.basename(p)
            out.put(("file", name))
            ok = up.upload(p)
            done += 1
            out.put(("done", (done, name, ok)))
        out.put(("finished", done))
    except Exception as e:                      # pragma: no cover - defensive
        out.put(("error", str(e)))


def run_gui(server, player, replay_dir=None, parent=None):
    """Show the sweep in a window. Returns when it has finished or been closed.

    Always marks the sweep done, including on cancel: anything not sent is
    still on disk and the normal watch picks it up, and a player who cancelled
    should not be shown this on every launch.
    """
    import tkinter as tk
    from uploader import Uploader, REPLAY_DIR

    up = Uploader(server, player, replay_dir or REPLAY_DIR)

    out = queue.Queue()
    stop = threading.Event()

    owns_root = parent is None
    root = tk.Tk() if owns_root else tk.Toplevel(parent)
    root.title("NTW3 Ladder - First upload")
    root.configure(bg="#10131C")
    root.minsize(560, 380)

    tk.Label(root, text="Searching for .replay files",
             bg="#10131C", fg="#E4C67E", font=("Georgia", 14, "bold")
             ).pack(pady=(18, 2))
    status = tk.Label(root, text="Reading your replay folder...",
                      bg="#10131C", fg="#CBC4B2", font=("Georgia", 10))
    status.pack()

    frame = tk.Frame(root, bg="#10131C")
    frame.pack(fill="both", expand=True, padx=16, pady=12)
    bar = tk.Scrollbar(frame)
    bar.pack(side="right", fill="y")
    log = tk.Text(frame, wrap="none", bg="#0B0E15", fg="#ECE6D7", relief="flat",
                  font=("Consolas", 9), yscrollcommand=bar.set, height=12,
                  padx=10, pady=8)
    log.pack(side="left", fill="both", expand=True)
    bar.config(command=log.yview)
    log.config(state="disabled")

    button = tk.Button(root, text="Cancel", width=16, bg="#171C29",
                       fg="#CBC4B2", relief="flat", font=("Georgia", 10),
                       command=lambda: stop.set())
    button.pack(pady=(0, 16))

    def say(line):
        log.config(state="normal")
        log.insert("end", line + "\n")
        log.see("end")
        log.config(state="disabled")

    state = {"total": 0, "sent": 0, "failed": 0, "over": False}

    def finish(text):
        state["over"] = True
        status.config(text=text)
        button.config(text="Close", command=root.destroy)

    def drain():
        try:
            while True:
                kind, payload = out.get_nowait()
                if kind == "total":
                    state["total"] = payload
                    if payload == 0:
                        say("No replay files found yet.")
                        finish("Nothing to upload. Waiting for your next "
                               "battle.")
                    else:
                        status.config(text=f"Uploading {payload} replay"
                                           f"{'' if payload == 1 else 's'}...")
                        say(f"Found {payload} replay files.\n")
                elif kind == "file":
                    say(f"  sending  {payload}")
                elif kind == "done":
                    n, name, ok = payload
                    if ok:
                        state["sent"] += 1
                    else:
                        state["failed"] += 1
                        say(f"  QUEUED   {name} (will retry later)")
                    status.config(text=f"{n} of {state['total']}")
                elif kind == "cancelled":
                    say("\nCancelled. Remaining files stay on disk and will "
                        "be picked up later.")
                    finish("Cancelled.")
                elif kind == "error":
                    say(f"\nProblem: {payload}")
                    finish("Stopped early.")
                elif kind == "finished":
                    ok, bad = state["sent"], state["failed"]
                    say(f"\nDone. {ok} uploaded"
                        + (f", {bad} queued for retry." if bad else "."))
                    finish("Finished. Now watching for new battles.")
        except queue.Empty:
            pass
        if not state["over"]:
            root.after(120, drain)

    # To the front: a window that opens behind the browser looks like the
    # program failing to start.
    try:
        root.lift()
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
        root.focus_force()
    except Exception:
        pass

    worker = threading.Thread(target=_sweep, args=(up, out, stop), daemon=True)
    worker.start()
    root.after(120, drain)

    if owns_root:
        root.mainloop()
    else:
        # Tk withdraws a transient window whose master is withdrawn, so only
        # make this transient when the parent is actually on screen.
        if parent.winfo_viewable():
            root.transient(parent)
        root.grab_set()
        parent.wait_window(root)

    # Stop the worker and wait for it before the interpreter is torn down.
    # Tcl requires the thread that created an async handler to destroy it; a
    # second thread alive across the teardown aborts the process with
    # "Tcl_AsyncDelete: async handler deleted by the wrong thread".
    stop.set()
    worker.join(timeout=15)

    try:
        root.destroy()
    except Exception:
        # Already destroyed by the Close button, which is the usual path.
        pass

    mark_done()
