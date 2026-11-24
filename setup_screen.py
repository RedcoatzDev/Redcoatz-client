"""The setup screen: a tracker from Steam to a fully patched NTW3.

One row per step, top to bottom: Steam, Napoleon: Total War, the four NTW3
files from the install list (ntw3setup), and Ready. Each row shows whether the
step is done, what it needs, and while it runs, a progress bar. Standard
installs every missing step in order; Custom lets the player pick.

Nothing happens until the player clicks Install and then says yes to a
question listing every file, its size, where it comes from and where its
checksum comes from. Steam and the game itself are only detected: their rows
offer a button that opens Steam, and the screen notices when Steam is done.

The work runs on a thread; the window reads its progress from a queue. All
detection, downloading and running is ntw3setup's; this module only draws.
"""

import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox

import ntw3setup

BG, PANEL, EDGE = "#10131C", "#171C29", "#2A3142"
FG, DIM, GOLD = "#ECE6D7", "#8A93A6", "#E4C67E"
GOOD, BAD, RED, RED_HI = "#7FBF6A", "#D9534F", "#AE3238", "#C9484A"

ICON = {"done": ("✓", GOOD), "todo": ("○", DIM), "active": ("●", GOLD),
        "failed": ("✗", BAD), "action": ("!", GOLD), "skip": ("–", DIM)}

STEAM_GET = "https://store.steampowered.com/about/"

#: The line under the title, in the Redcoatz team's words.
INTRO = ("This third party installer makes it easier for new players to get access "
         "to the NTW3 mod. This installer is a convenience feature, you can also "
         "download each pack individually yourself if you prefer.")


def gb(n):
    return "%.1f GB" % (n / 1e9) if n >= 1e9 else "%d MB" % max(1, n // 1_000_000)


class SetupScreen:
    """The window. `loader()` returns the install list (ntw3setup.fetch_manifest
    by default) and runs on a thread so a slow GitHub does not freeze it.
    `on_close(ready)` is called once when the window goes, with whether every
    step is installed."""

    def __init__(self, parent, loader=None, engine=ntw3setup, on_close=None,
                 title="NTW3 Installer by Redcoatz", log=print):
        self.e = engine
        self.loader = loader or engine.fetch_manifest
        self.on_close = on_close
        self.log = log
        self.manifest = None
        self.game = None
        self.states = []
        self.running = False
        self.stop = False
        self.q = queue.Queue()
        self.rows = {}
        self.choice = {}

        self.win = win = tk.Toplevel(parent) if parent is not None else tk.Tk()
        win.title(title)
        win.configure(bg=BG)
        win.minsize(660, 520)
        win.protocol("WM_DELETE_WINDOW", self.close)
        if parent is not None and not parent.winfo_viewable():
            win.attributes("-topmost", True)
            win.lift()

        tk.Label(win, text=title, bg=BG, fg=GOLD, font=("Georgia", 16, "bold")).pack(
            anchor="w", padx=26, pady=(22, 2))
        self.sub = tk.Label(win, text="Reading the install list from GitHub...", bg=BG, fg=DIM,
                            font=("Georgia", 10), justify="left", anchor="w", wraplength=600)
        self.sub.pack(anchor="w", padx=26, pady=(0, 12))

        self.mode = tk.StringVar(value="standard")
        modes = tk.Frame(win, bg=BG)
        modes.pack(anchor="w", padx=22, pady=(0, 8))
        for value, text in (("standard", "Standard install (recommended)"), ("custom", "Custom")):
            tk.Radiobutton(modes, text=text, value=value, variable=self.mode, command=self.redraw,
                           bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                           activeforeground=GOLD, font=("Georgia", 10)).pack(side="left", padx=(0, 18))
        self.modes = modes

        self.list = tk.Frame(win, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
        self.list.pack(fill="x", padx=26)

        self.summary = tk.Label(win, text="", bg=BG, fg=FG, font=("Georgia", 10),
                                justify="left", anchor="w", wraplength=600)
        self.summary.pack(anchor="w", padx=26, pady=(12, 0))
        self.error = tk.Label(win, text="", bg=BG, fg=BAD, font=("Georgia", 10),
                              justify="left", anchor="w", wraplength=600)
        self.error.pack(anchor="w", padx=26)

        buttons = tk.Frame(win, bg=BG)
        buttons.pack(anchor="e", padx=26, pady=(12, 20))
        self.go = self._button(buttons, "Install", self.install, primary=True)
        self.go.pack(side="left", padx=6)
        self.quit = self._button(buttons, "Close", self.close)
        self.quit.pack(side="left", padx=6)

        threading.Thread(target=self._load, daemon=True, name="setup-load").start()
        self._poll()

    # ---- building blocks -------------------------------------------------

    def _button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief="flat", cursor="hand2",
                         bg=RED if primary else PANEL, fg="#FFFBF4" if primary else "#CBC4B2",
                         activebackground=RED_HI if primary else EDGE,
                         font=("Georgia", 10, "bold" if primary else "normal"), padx=14, pady=4)

    def _row(self, key, title):
        f = tk.Frame(self.list, bg=PANEL)
        f.pack(fill="x", padx=14, pady=6)
        icon = tk.Label(f, text="○", bg=PANEL, fg=DIM, font=("Segoe UI Symbol", 14), width=2)
        icon.grid(row=0, column=0, rowspan=2, sticky="n")
        var = tk.BooleanVar(value=True)
        box = tk.Checkbutton(f, variable=var, bg=PANEL, fg=GOLD, activebackground=PANEL,
                             activeforeground=GOLD, selectcolor=BG,
                             command=lambda k=key: self._toggle(k))
        name = tk.Label(f, text=title, bg=PANEL, fg=FG, font=("Georgia", 11, "bold"), anchor="w")
        name.grid(row=0, column=2, sticky="w")
        detail = tk.Label(f, text="", bg=PANEL, fg=DIM, font=("Georgia", 9), anchor="w",
                          justify="left", wraplength=430)
        detail.grid(row=1, column=2, sticky="w")
        bar = tk.Canvas(f, height=6, bg=BG, highlightthickness=0, width=430)
        action = tk.Frame(f, bg=PANEL)
        action.grid(row=0, column=3, rowspan=2, sticky="e", padx=(8, 0))
        f.grid_columnconfigure(2, weight=1)
        self.rows[key] = {"frame": f, "icon": icon, "box": box, "name": name, "detail": detail,
                          "bar": bar, "action": action, "bar_shown": False}
        self.choice[key] = var
        return self.rows[key]

    def _set(self, key, state, detail=None):
        r = self.rows[key]
        glyph, colour = ICON[state]
        r["icon"].config(text=glyph, fg=colour)
        r["name"].config(fg=DIM if state in ("todo", "skip") else FG)
        if detail is not None:
            r["detail"].config(text=detail)

    def _bar(self, key, fraction):
        r = self.rows[key]
        c = r["bar"]
        if fraction is None:
            if r["bar_shown"]:
                c.grid_forget()
                r["bar_shown"] = False
            return
        if not r["bar_shown"]:
            c.grid(row=2, column=2, sticky="we", pady=(4, 0))
            r["bar_shown"] = True
        c.update_idletasks()
        w = max(c.winfo_width(), 1)
        c.delete("all")
        c.create_rectangle(0, 0, w, 6, fill=EDGE, width=0)
        c.create_rectangle(0, 0, int(w * max(0.0, min(1.0, fraction))), 6, fill=GOLD, width=0)

    def _action(self, key, text=None, command=None):
        a = self.rows[key]["action"]
        for w in a.winfo_children():
            w.destroy()
        if text:
            b = self._button(a, text, command, primary=True)
            b.pack()

    # ---- loading and detecting -------------------------------------------

    def _load(self):
        try:
            self.q.put(("manifest", self.loader()))
        except Exception as e:                  # noqa: BLE001
            self.q.put(("load_error", str(e) or type(e).__name__))

    def _build_rows(self):
        self._row("steam", "Steam")
        self._row("napoleon", "Napoleon: Total War")
        for s in self.manifest["steps"]:
            self._row(s["id"], s["title"])
        self._row("ready", "Ready to play")

    def detect(self):
        """Look at the machine again and redraw. Cheap: sizes, and a CRC of a
        few small files."""
        if not self.manifest:
            return
        roots = self.e.steam_roots()
        nap, self.game = self.e.napoleon(roots)
        self.steam_ok = bool(roots)
        self.nap_state = nap
        self.states = self.e.detect(self.manifest, self.game) if nap == "installed" else \
            [(s, self.e.MISSING if i == 0 else self.e.BLOCKED) for i, s in enumerate(self.manifest["steps"])]
        self.redraw()

    def redraw(self):
        if not self.manifest or self.running:
            return
        custom = self.mode.get() == "custom"
        if self.steam_ok:
            self._set("steam", "done", "Installed")
            self._action("steam")
        else:
            self._set("steam", "action", "Steam is needed to install and run Napoleon: Total War.")
            self._action("steam", "Get Steam", lambda: webbrowser.open_new_tab(STEAM_GET))
        if self.nap_state == "installed":
            self._set("napoleon", "done", "Installed: %s" % self.game)
            self._action("napoleon")
        elif self.nap_state == "updating":
            self._set("napoleon", "action", "Steam is still installing or updating it. "
                                            "This screen notices when it is done.")
            self._action("napoleon")
        else:
            self._set("napoleon", "action", "Buy or install the Definitive Edition in Steam. "
                                            "This screen notices when it is done.")
            self._action("napoleon", "Open in Steam", self._open_steam)

        chosen = self._chosen()
        for s, st in self.states:
            key = s["id"]
            r = self.rows[key]
            self._bar(key, None)
            self._action(key)
            if s.get("unpacked_bytes"):
                size = "%s download, %s on disk" % (gb(s["download"]["bytes"]), gb(s["unpacked_bytes"]))
            else:
                size = "%s download, rewrites the packs in place (needs %s free while it runs)" % (
                    gb(s["download"]["bytes"]), gb(s.get("work_bytes") or 0))
            if st == self.e.INSTALLED:
                self._set(key, "done", "Installed")
            elif st == self.e.UNKNOWN:
                self._set(key, "failed", "The packs are not ones this repair recognises, so it "
                                         "would refuse. Reinstalling 9.4 to 9.6 fixes that.")
            elif st == self.e.PARTIAL:
                self._set(key, "todo", "Incomplete: some files are missing or changed. "
                                       "Installing it again repairs it. " + size)
            elif custom and key not in chosen:
                self._set(key, "skip", "Not selected. " + size)
            else:
                self._set(key, "todo", size)
            if custom and st != self.e.INSTALLED:
                r["box"].grid(row=0, column=1, rowspan=2, sticky="n")
            else:
                r["box"].grid_forget()
        all_done = bool(self.states) and all(st == self.e.INSTALLED for _, st in self.states)
        self._set("ready", "done" if all_done else "todo",
                  "NTW3 9.6.1 is installed. Redcoatz applies its crash fixes when you launch."
                  if all_done else "Redcoatz applies its crash fixes when you launch.")
        self._summarise(all_done)

    def _toggle(self, key):
        """Keep a custom selection installable: the patches only stack in
        order, so ticking a step ticks the missing ones before it, and
        unticking one unticks the ones after it."""
        ids = [s["id"] for s, st in self.states if st != self.e.INSTALLED]
        if key in ids:
            i = ids.index(key)
            if self.choice[key].get():
                for k in ids[:i]:
                    self.choice[k].set(True)
            else:
                for k in ids[i + 1:]:
                    self.choice[k].set(False)
        self.redraw()

    def _chosen(self):
        if self.mode.get() != "custom":
            return None
        return [s["id"] for s, st in self.states if st != self.e.INSTALLED and self.choice[s["id"]].get()]

    def _plan(self):
        return self.e.todo(self.states, self._chosen()) if self.states else []

    def _summarise(self, all_done):
        plan = self._plan()
        if all_done:
            self.summary.config(text="Everything is installed.")
            self.go.config(text="Continue", command=self.close, state="normal")
            self.modes.pack_forget()
            return
        self.go.config(text="Install", command=self.install)
        if self.nap_state != "installed":
            self.summary.config(text="Install Napoleon: Total War in Steam first.")
            self.go.config(state="disabled")
            return
        if not plan:
            self.summary.config(text="Nothing selected.")
            self.go.config(state="disabled")
            return
        down = sum(s["download"]["bytes"] for s in plan)
        need = sum(self.e.bytes_needed(s) for s in plan)
        try:
            free = self.e.free_bytes(self.game)
        except OSError:
            free = 0
        extra = [s["title"] for s in plan if self._chosen() is not None and s["id"] not in self._chosen()]
        text = "%d step%s: %s to download, needs %s free on the game's drive (%s free)." % (
            len(plan), "" if len(plan) == 1 else "s", gb(down), gb(need), gb(free))
        if extra:
            text += "\nAlso included, because the selection needs it: %s." % ", ".join(extra)
        self.summary.config(text=text)
        self.go.config(state="normal")

    def _open_steam(self):
        try:
            os.startfile("steam://install/%d" % self.e.STEAM_APP)
        except (OSError, AttributeError):
            webbrowser.open_new_tab(self.manifest.get("game", {}).get("store_url") or STEAM_GET)

    # ---- installing ---------------------------------------------------------

    def install(self):
        plan = self._plan()
        if not plan:
            return
        problem = self.e.check_room(plan, self.game)
        if problem:
            self.error.config(text=problem)
            return
        if not messagebox.askyesno("Install NTW3", self.e.confirm_text(plan, self.manifest),
                                   parent=self.win):
            self.log("setup: the player declined")
            return
        self.error.config(text="")
        self.running, self.stop = True, False
        self.modes.pack_forget()
        for r in self.rows.values():
            r["box"].grid_forget()
        self.go.config(text="Stop", command=self._ask_stop, state="normal")
        self.quit.config(state="disabled")
        threading.Thread(target=self._work, args=(plan,), daemon=True, name="setup-work").start()

    def _ask_stop(self):
        self.stop = True
        self.go.config(state="disabled", text="Stopping...")

    def _work(self, plan):
        for step in plan:
            key = step["id"]
            self.q.put(("start", key))
            began = time.monotonic()

            def progress(phase, done, total, key=key, began=began):
                self.q.put(("progress", key, phase, done, total, time.monotonic() - began))
            try:
                if self.stop:
                    raise self.e.SetupError("Stopped before this step. Install continues from here next time.")
                self.e.do_step(step, self.game, progress=progress, stop=lambda: self.stop, log=self.log)
                self.q.put(("done", key))
            except self.e.SetupError as err:
                self.q.put(("failed", key, str(err)))
                break
            except Exception as err:            # noqa: BLE001
                self.q.put(("failed", key, "Unexpected error: %r" % err))
                break
        self.e.cleanup(self.game)
        self.q.put(("finished",))

    def _progress(self, key, phase, done, total, elapsed):
        if phase == "download":
            if total and done >= total:
                text = "Checking the download against GitHub's checksum..."
                frac = 1.0
            else:
                rate = done / max(elapsed, 0.001)
                left = (total - done) / rate if rate > 0 else 0
                text = "Downloading %s of %s  (%.1f MB/s, about %s left)" % (
                    gb(done), gb(total), rate / 1e6,
                    "%d min" % (left // 60 + 1) if left >= 60 else "%d s" % max(1, left))
                frac = done / total if total else 0
        else:
            if total:
                text = "Installing: %s of %s written" % (gb(done), gb(total))
                frac = done / total
            else:
                text = "Running the texture repair. This takes a few minutes."
                frac = None
        self._set(key, "active", text)
        self._bar(key, frac)

    # ---- the loop -----------------------------------------------------------

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "manifest":
                    self.manifest = msg[1]
                    self.sub.config(text=INTRO)
                    self._build_rows()
                    self.detect()
                elif kind == "load_error":
                    self.sub.config(text="The install list could not be read from GitHub.")
                    self.error.config(text=msg[1] + "\n\nCheck the connection and open this screen again.")
                    self.go.config(state="disabled")
                elif kind == "start":
                    self._set(msg[1], "active", "Starting...")
                elif kind == "progress":
                    self._progress(*msg[1:])
                elif kind == "done":
                    self._bar(msg[1], None)
                    self._set(msg[1], "done", "Installed")
                elif kind == "failed":
                    self._bar(msg[1], None)
                    self._set(msg[1], "failed", "Did not finish.")
                    self.error.config(text=msg[2])
                elif kind == "finished":
                    self.running = False
                    self.quit.config(state="normal")
                    self.go.config(state="normal")
                    self.detect()
        except queue.Empty:
            pass
        # While the game is missing, keep looking: the player is in Steam.
        if (self.manifest and not self.running and getattr(self, "nap_state", "installed") != "installed"
                and time.monotonic() - getattr(self, "_last_look", 0) > 3):
            self._last_look = time.monotonic()
            self.detect()
        if self.win.winfo_exists():
            self.win.after(120, self._poll)

    def close(self):
        if self.running:
            if not messagebox.askyesno("Install NTW3", "An install is running. Stop it and close? "
                                       "A download resumes where it stopped next time.", parent=self.win):
                return
            self.stop = True
        ready = bool(self.states) and all(st == self.e.INSTALLED for _, st in self.states)
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close(ready)
