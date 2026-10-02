"""The GameLens window.

A thin layer over the engine in app.py: a Start/Stop button, the subtitles as
they are read, the questions and answers, usage and capture counters, and
buttons for the screenshots folder, calibration, settings and the API key.

The engine runs on background threads and reports through events; those go
into a queue that the window drains on its own thread, because tkinter must
only ever be touched from the thread that created it.

Run with:  python -m gamelens.gui     (or GameLens.exe once packaged)
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .config import Config, app_dir, default_config_path
from .hotkeys import is_admin

# -- look -------------------------------------------------------------------

BG = "#15171c"
PANEL = "#1d2027"
PANEL_2 = "#242832"
BORDER = "#2e3340"
TEXT = "#e6e8ee"
MUTED = "#8b91a0"
ACCENT = "#6aa8ff"
GOOD = "#4ade80"
WARN = "#f5b74a"
BAD = "#f87171"
VIOLET = "#c084fc"

STATE_LOOK = {
    "stopped": (MUTED, "Stopped"),
    "starting": (WARN, "Starting…"),
    "ready": (GOOD, "Watching the screen"),
    "listening": (BAD, "Listening…"),
    "transcribing": (WARN, "Understanding your question…"),
    "thinking": (ACCENT, "Thinking…"),
    "speaking": (VIOLET, "Speaking"),
    "error": (BAD, "Problem - see below"),
}

ENV_KEY = "ANTHROPIC_API_KEY"


def _font_family(root: tk.Misc) -> str:
    from tkinter import font as tkfont

    available = set(tkfont.families(root))
    for name in ("Segoe UI", "Inter", "Helvetica Neue", "DejaVu Sans", "Arial"):
        if name in available:
            return name
    return "TkDefaultFont"


def pretty_combo(spec: str | None) -> str:
    """'<ctrl>+<alt>+q' -> 'Ctrl+Alt+Q', '<mouse5>' -> 'Mouse 5'."""
    if not spec:
        return ""
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win",
             "win": "Win", "space": "Space", "esc": "Esc", "enter": "Enter"}
    parts = []
    for token in str(spec).split("+"):
        t = token.strip().strip("<>").lower()
        if t.startswith("mouse") and t[5:].isdigit():
            parts.append(f"Mouse {t[5:]}")
        else:
            parts.append(names.get(t, t.upper() if len(t) <= 3 else t.capitalize()))
    return "+".join(parts)


def _open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _self_command(*cli_args: str) -> list[str]:
    """A command line that runs GameLens itself, packaged or from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--cli", *cli_args]
    return [sys.executable, "-m", "gamelens", *cli_args]


# -- .env handling ------------------------------------------------------------


def read_env_key(env_path: Path) -> str:
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith(ENV_KEY + "="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                return "" if "your-key-here" in value else value
    except OSError:
        pass
    return os.environ.get(ENV_KEY, "")


def write_env_key(env_path: Path, key: str) -> None:
    lines: list[str] = []
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    lines = [ln for ln in lines if not ln.strip().startswith(ENV_KEY + "=")]
    lines.append(f"{ENV_KEY}={key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[ENV_KEY] = key


def mask_key(key: str) -> str:
    if not key:
        return "not set"
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "set"


# -- the window -----------------------------------------------------------------


class GameLensWindow:
    def __init__(self, root: tk.Tk, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.base = config_path.parent
        self.env_path = self.base / ".env"
        self.engine = None
        self.events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.busy = False

        family = _font_family(root)
        self.f_body = (family, 10)
        self.f_small = (family, 9)
        self.f_head = (family, 11, "bold")
        self.f_title = (family, 16, "bold")
        self.f_big = (family, 16, "bold")
        self.f_mono = ("Consolas", 10) if sys.platform == "win32" else ("DejaVu Sans Mono", 10)

        root.title("GameLens")
        root.configure(bg=BG)
        root.geometry("1060x760")
        root.minsize(900, 680)
        self._style()
        self._build()
        self._refresh_key_status()
        self._refresh_counters()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._pump)
        root.after(1000, self._tick)

    # -- styling ---------------------------------------------------------------

    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT, font=self.f_body)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Bg.TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED,
                        font=self.f_small)
        style.configure("Head.TLabel", background=PANEL, foreground=TEXT,
                        font=self.f_head)
        style.configure("Value.TLabel", background=PANEL, foreground=TEXT,
                        font=self.f_body)
        style.configure("Big.TLabel", background=PANEL, foreground=TEXT,
                        font=self.f_big)
        style.configure("TButton", background=PANEL_2, foreground=TEXT,
                        bordercolor=BORDER, focuscolor=PANEL_2, padding=(10, 6),
                        relief="flat")
        style.map("TButton", background=[("active", BORDER), ("disabled", PANEL)],
                  foreground=[("disabled", MUTED)])
        style.configure("Start.TButton", background="#2563eb", foreground="white",
                        font=self.f_head, padding=(18, 9))
        style.map("Start.TButton", background=[("active", "#1d4ed8"),
                                               ("disabled", "#334155")])
        style.configure("Stop.TButton", background="#b91c1c", foreground="white",
                        font=self.f_head, padding=(18, 9))
        style.map("Stop.TButton", background=[("active", "#991b1b"),
                                              ("disabled", "#334155")])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT,
                        insertcolor=TEXT, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, padding=6)
        style.map("TEntry", bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)])

    def _panel(self, parent, title: str | None = None) -> ttk.Frame:
        outer = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 10))
        if title:
            ttk.Label(outer, text=title, style="Head.TLabel").pack(anchor="w",
                                                                   pady=(0, 6))
        return outer

    def _textbox(self, parent, height: int) -> tk.Text:
        box = tk.Text(parent, height=height, wrap="word", bg=PANEL_2, fg=TEXT,
                      insertbackground=TEXT, relief="flat", borderwidth=0,
                      highlightthickness=1, highlightbackground=BORDER,
                      highlightcolor=BORDER, padx=10, pady=8, font=self.f_body)
        box.tag_configure("time", foreground=MUTED, font=self.f_small)
        box.tag_configure("you", foreground=ACCENT, font=self.f_head)
        box.tag_configure("bot", foreground=VIOLET, font=self.f_head)
        box.tag_configure("error", foreground=BAD)
        box.tag_configure("info", foreground=MUTED)
        box.configure(state="disabled")
        return box

    # -- layout ----------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # Header: title, status, start/stop
        header = ttk.Frame(root, style="Bg.TFrame", padding=(18, 14, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="GameLens", font=self.f_title).grid(row=0, column=0,
                                                                   sticky="w")
        status = ttk.Frame(header, style="Bg.TFrame")
        status.grid(row=0, column=1, sticky="w", padx=(18, 0))
        self.status_dot = tk.Canvas(status, width=12, height=12, bg=BG,
                                    highlightthickness=0)
        self.status_dot.pack(side="left")
        self.status_circle = self.status_dot.create_oval(1, 1, 11, 11, fill=MUTED,
                                                         outline="")
        self.status_text = ttk.Label(status, text="Stopped", foreground=MUTED)
        self.status_text.pack(side="left", padx=(8, 0))
        self.start_btn = ttk.Button(header, text="▶  Start", style="Start.TButton",
                                    command=self.toggle)
        self.start_btn.grid(row=0, column=2, sticky="e")

        # Admin banner (only when it matters)
        self.banner = None
        if sys.platform == "win32" and is_admin() is False:
            self.banner = tk.Frame(root, bg="#3b2a12", padx=14, pady=8)
            self.banner.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
            tk.Label(self.banner, bg="#3b2a12", fg=WARN, font=self.f_body,
                     text="Not running as administrator - hotkeys will not work "
                          "while the game has focus.").pack(side="left")
            ttk.Button(self.banner, text="Restart as administrator",
                       command=self.restart_as_admin).pack(side="right")

        # Body: left = subtitles + conversation, right = sidebar
        body = ttk.Frame(root, style="Bg.TFrame", padding=(18, 0, 18, 10))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0, minsize=320)
        body.rowconfigure(0, weight=2)
        body.rowconfigure(1, weight=3)

        subs = self._panel(body, "Subtitles")
        subs.grid(row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        self.subs_box = self._textbox(subs, height=8)
        self.subs_box.pack(fill="both", expand=True)
        self._append(self.subs_box, "Press Start, then play. Lines appear here as "
                     "they are read from the screen.", "info")

        convo = self._panel(body, "Questions and answers")
        convo.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.convo_box = self._textbox(convo, height=10)
        self.convo_box.pack(fill="both", expand=True)
        ask_row = ttk.Frame(convo, style="Panel.TFrame")
        ask_row.pack(fill="x", pady=(10, 0))
        self.ask_entry = ttk.Entry(ask_row)
        self.ask_entry.pack(side="left", fill="x", expand=True)
        self.ask_entry.bind("<Return>", lambda _e: self.ask_typed())
        self._placeholder(self.ask_entry, "Or type a question here…")
        ttk.Button(ask_row, text="Ask", command=self.ask_typed).pack(side="left",
                                                                     padx=(8, 0))
        self._hotkey_hint = ttk.Label(convo, style="Muted.TLabel", text="")
        self._hotkey_hint.pack(anchor="w", pady=(8, 0))

        side = ttk.Frame(body, style="Bg.TFrame")
        side.grid(row=0, column=1, rowspan=2, sticky="nsew")
        side.columnconfigure(0, weight=1)

        # Usage card
        usage = self._panel(side, "API usage")
        usage.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.cost_session = ttk.Label(usage, text="$0.000", style="Big.TLabel")
        self.cost_session.pack(anchor="w")
        ttk.Label(usage, text="estimated cost this session",
                  style="Muted.TLabel").pack(anchor="w")
        self.usage_grid = ttk.Frame(usage, style="Panel.TFrame")
        self.usage_grid.pack(fill="x", pady=(6, 0))
        self.usage_vals = self._rows(self.usage_grid, [
            ("questions", "Questions"),
            ("tokens", "Tokens in / out"),
            ("total", "All time"),
        ])

        # Capture card
        cap = self._panel(side, "Screen capture")
        cap.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.cap_vals = self._rows(cap, [
            ("lines", "Subtitle lines read"),
            ("checked", "Frames checked"),
            ("ocr", "Text recognitions run"),
            ("saved", "Skipped as unchanged"),
            ("shots", "Screenshots on disk"),
        ])

        # Actions
        actions = self._panel(side, "Tools")
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        tools = ttk.Frame(actions, style="Panel.TFrame")
        tools.pack(fill="x")
        tools.columnconfigure(0, weight=1, uniform="t")
        tools.columnconfigure(1, weight=1, uniform="t")
        for i, (label, command) in enumerate((
            ("Screenshots", self.open_screenshots),
            ("Calibrate", self.calibrate),
            ("Settings", self.open_settings),
            ("Debug log", self.open_log),
        )):
            ttk.Button(tools, text=label, command=command).grid(
                row=i // 2, column=i % 2, sticky="ew",
                padx=(0, 6) if i % 2 == 0 else (0, 0), pady=(0, 6))

        # API key
        keycard = self._panel(side, "Anthropic API key")
        keycard.grid(row=3, column=0, sticky="ew")
        status_row = ttk.Frame(keycard, style="Panel.TFrame")
        status_row.pack(fill="x")
        self.key_status = ttk.Label(status_row, style="Muted.TLabel", text="",
                                    wraplength=230)
        self.key_status.pack(side="left")
        self.key_change = ttk.Button(status_row, text="Change",
                                     command=lambda: self._show_key_entry(True))
        self.key_row = ttk.Frame(keycard, style="Panel.TFrame")
        self.key_entry = ttk.Entry(self.key_row, show="•")
        self.key_entry.pack(side="left", fill="x", expand=True)
        self.key_entry.bind("<Return>", lambda _e: self.save_key())
        ttk.Button(self.key_row, text="Save", command=self.save_key).pack(
            side="left", padx=(8, 0))

        # Status bar
        self.statusbar = tk.Label(root, bg=PANEL, fg=MUTED, anchor="w",
                                  font=self.f_small, padx=18, pady=5, text="")
        self.statusbar.grid(row=3, column=0, sticky="ew")
        self._update_hotkey_hint()

    def _placeholder(self, entry: ttk.Entry, hint: str) -> None:
        def show(_e=None):
            if not entry.get():
                entry.insert(0, hint)
                entry.configure(foreground=MUTED)

        def hide(_e=None):
            if entry.get() == hint and str(entry.cget("foreground")) == MUTED:
                entry.delete(0, "end")
                entry.configure(foreground=TEXT)

        entry.bind("<FocusIn>", hide, add="+")
        entry.bind("<FocusOut>", show, add="+")
        entry._hint = hint  # type: ignore[attr-defined]
        show()

    def _rows(self, parent, rows: list[tuple[str, str]]) -> dict[str, ttk.Label]:
        grid = ttk.Frame(parent, style="Panel.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        values = {}
        for i, (key, label) in enumerate(rows):
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(
                row=i, column=0, sticky="w", pady=2)
            val = ttk.Label(grid, text="—", style="Value.TLabel")
            val.grid(row=i, column=1, sticky="e", pady=2)
            values[key] = val
        return values

    # -- text helpers -------------------------------------------------------------

    def _append(self, box: tk.Text, text: str, tag: str | None = None,
                stamp: bool = False, limit: int = 400) -> None:
        box.configure(state="normal")
        if stamp:
            box.insert("end", time.strftime("%H:%M:%S  "), "time")
        box.insert("end", text + "\n", tag or ())
        # Keep the widget from growing forever.
        lines = int(box.index("end-1c").split(".")[0])
        if lines > limit:
            box.delete("1.0", f"{lines - limit}.0")
        box.see("end")
        box.configure(state="disabled")

    def _say(self, who: str, text: str) -> None:
        self.convo_box.configure(state="normal")
        self.convo_box.insert("end", time.strftime("%H:%M  "), "time")
        self.convo_box.insert("end", f"{who}  ", "you" if who == "You" else "bot")
        self.convo_box.insert("end", text + "\n\n")
        self.convo_box.see("end")
        self.convo_box.configure(state="disabled")

    # -- state --------------------------------------------------------------------

    def _set_status(self, state: str) -> None:
        colour, label = STATE_LOOK.get(state, (MUTED, state))
        self.status_dot.itemconfigure(self.status_circle, fill=colour)
        self.status_text.configure(text=label, foreground=colour)
        self._sync_button()

    def _sync_button(self) -> None:
        """The button follows whether an engine exists, not the status text."""
        if self.busy:
            starting = self.engine is None or not getattr(self.engine, "_started", False)
            self.start_btn.configure(text="Starting…" if starting else "Stopping…",
                                     style="Start.TButton", state="disabled")
        elif self.engine is not None:
            self.start_btn.configure(text="■  Stop", style="Stop.TButton",
                                     state="normal")
        else:
            self.start_btn.configure(text="▶  Start", style="Start.TButton",
                                     state="normal")

    def _update_hotkey_hint(self) -> None:
        try:
            keys = Config.load(self.config_path).hotkeys
            verb = "Tap" if str(keys.get("mode", "hold")) == "toggle" else "Hold"
            self._hotkey_hint.configure(
                text=f"{verb} {pretty_combo(keys.get('talk'))} to ask out loud  ·  "
                     f"{pretty_combo(keys.get('repeat'))} explains the last line  ·  "
                     f"{pretty_combo(keys.get('stop'))} stops")
        except Exception:
            pass

    # -- engine lifecycle -------------------------------------------------------

    def toggle(self) -> None:
        if self.busy:
            return
        if self.engine is not None:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        self.busy = True
        self._set_status("starting")
        self.statusbar.configure(text="Loading speech recognition and checking "
                                      "the API key - a few seconds…")

        def work() -> None:
            try:
                from .__main__ import load_env
                from .app import GameLens

                load_env(self.config_path)
                config = Config.load(self.config_path)
                engine = GameLens(config)
                engine.on(lambda kind, data: self.events.put((kind, data)))
                self.engine = engine
                ok = engine.start()
                self.events.put(("_started", {"ok": ok}))
            except Exception as exc:
                from .app import friendly_error

                self.engine = None
                self.events.put(("error", {"message": friendly_error(exc)}))
                self.events.put(("_started", {"ok": False}))

        threading.Thread(target=work, name="gamelens-gui-start", daemon=True).start()

    def stop(self) -> None:
        engine, self.engine = self.engine, None
        self.busy = True
        self.start_btn.configure(state="disabled", text="Stopping…")
        self.status_text.configure(text="Stopping…", foreground=MUTED)

        def work() -> None:
            try:
                if engine is not None:
                    engine.stop()
            finally:
                self.events.put(("_stopped", {}))

        threading.Thread(target=work, name="gamelens-gui-stop", daemon=True).start()

    # -- event pump -------------------------------------------------------------

    def _pump(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _handle(self, kind: str, data: dict) -> None:
        if kind == "status":
            self._set_status(data.get("state", ""))
        elif kind == "subtitle":
            self._append(self.subs_box, data.get("text", ""), stamp=True)
        elif kind == "question":
            self._say("You", data.get("text", ""))
        elif kind == "answer":
            self._say("GameLens", data.get("text", ""))
        elif kind == "error":
            self._append(self.convo_box, "⚠ " + data.get("message", ""), "error")
        elif kind == "usage":
            self._show_usage(data)
        elif kind == "log":
            self.statusbar.configure(text=data.get("line", "")[:160])
        elif kind == "_started":
            self.busy = False
            if data.get("ok"):
                self.subs_box.configure(state="normal")
                self.subs_box.delete("1.0", "end")
                self.subs_box.configure(state="disabled")
                self._append(self.convo_box, "Started - go play. Ask whenever you "
                             "need.", "info")
            else:
                self.engine = None
                self._set_status("stopped")
            self._sync_button()
        elif kind == "_stopped":
            self.busy = False
            self._set_status("stopped")
            self.statusbar.configure(text="Stopped.")
        elif kind == "_calibrated":
            self._append(self.convo_box, data.get("message", ""),
                         "error" if data.get("failed") else "info")

    # -- counters -----------------------------------------------------------------

    def _show_usage(self, usage: dict) -> None:
        session, total = usage.get("session", {}), usage.get("total", {})
        self.cost_session.configure(text=f"${session.get('cost', 0):.3f}")
        self.usage_vals["questions"].configure(text=str(session.get("questions", 0)))
        self.usage_vals["tokens"].configure(
            text=f"{session.get('input', 0):,} / {session.get('output', 0):,}")
        self.usage_vals["total"].configure(
            text=f"{total.get('questions', 0)} q · ${total.get('cost', 0):.2f}")

    def _refresh_counters(self) -> None:
        engine = self.engine
        if engine is not None:
            try:
                stats = engine.stats()
            except Exception:
                return
            self._show_usage(stats["usage"])
            checked = stats["frames_checked"]
            skipped = stats["frames_skipped"]
            pct = f" ({skipped * 100 // checked}%)" if checked else ""
            self.cap_vals["lines"].configure(text=f"{stats['lines_read']:,}")
            self.cap_vals["checked"].configure(text=f"{checked:,}")
            self.cap_vals["ocr"].configure(text=f"{stats['ocr_runs']:,}")
            self.cap_vals["saved"].configure(text=f"{skipped:,}{pct}")
            self.cap_vals["shots"].configure(
                text=f"{stats['crops_on_disk']} lines · {stats['frames_on_disk']} frames")
        else:
            # Not running: still show all-time usage from disk.
            try:
                from .usage import UsageTracker

                self._show_usage(UsageTracker(self.base / "usage.json").snapshot())
            except Exception:
                pass

    def _tick(self) -> None:
        self._refresh_counters()
        self.root.after(1000, self._tick)

    # -- buttons --------------------------------------------------------------------

    def ask_typed(self) -> None:
        question = self.ask_entry.get().strip()
        if not question or question == getattr(self.ask_entry, "_hint", None):
            return
        if self.engine is None or not self.engine._started:
            self._append(self.convo_box, "Press Start first.", "error")
            return
        self.ask_entry.delete(0, "end")
        self.engine.ask(question)

    def open_screenshots(self) -> None:
        try:
            config = Config.load(self.config_path)
            folder = Path(str(config.screenshots.get("folder", "screenshots")))
            folder = folder if folder.is_absolute() else self.base / folder
            for sub in ("subtitles", "frames"):
                (folder / sub).mkdir(parents=True, exist_ok=True)
            _open_path(folder)
        except Exception as exc:
            messagebox.showerror("GameLens", f"Could not open the folder:\n{exc}")

    def open_settings(self) -> None:
        if not self.config_path.exists():
            Config.load(self.config_path).save()
        _open_path(self.config_path)

    def open_log(self) -> None:
        log = self.base / "gamelens-debug.log"
        if not log.exists():
            log.write_text("", encoding="utf-8")
        _open_path(log)

    def calibrate(self) -> None:
        if not messagebox.askokcancel(
            "Calibrate subtitle box",
            "After you press OK you have 6 seconds to switch to the game.\n\n"
            "Make sure a line of dialogue is on screen when the time runs out. "
            "A window then opens: drag a box around the subtitle and press Enter.",
        ):
            return
        was_running = self.engine is not None
        if was_running:
            self.stop()
        self.root.iconify()

        def work() -> None:
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     if sys.platform == "win32" else 0)
            try:
                proc = subprocess.run(
                    _self_command("--config", str(self.config_path),
                                  "calibrate", "--delay", "6"),
                    cwd=str(self.base), capture_output=True, text=True,
                    timeout=600, creationflags=flags,
                )
                output = proc.stdout + proc.stderr
                read_back = next((ln.strip() for ln in output.splitlines()
                                  if "Read back:" in ln), "")
                if proc.returncode == 0:
                    msg = "Subtitle box saved. " + (read_back or "")
                    if was_running:
                        msg += "  Press Start to use it."
                    self.events.put(("_calibrated", {"message": msg}))
                else:
                    tail = output.strip().splitlines()[-1:] or ["cancelled"]
                    self.events.put(("_calibrated", {
                        "message": f"Calibration not saved ({tail[0]}).",
                        "failed": True}))
            except Exception as exc:
                self.events.put(("_calibrated", {
                    "message": f"Calibration failed: {exc}", "failed": True}))
            finally:
                self.root.after(0, self.root.deiconify)

        threading.Thread(target=work, name="gamelens-calibrate", daemon=True).start()

    def _show_key_entry(self, visible: bool) -> None:
        if visible:
            self.key_change.pack_forget()
            self.key_row.pack(fill="x", pady=(6, 0))
            self.key_entry.focus_set()
        else:
            self.key_row.pack_forget()
            self.key_change.pack(side="right")

    def _refresh_key_status(self) -> None:
        key = read_env_key(self.env_path)
        self.key_status.configure(
            text=f"Saved: {mask_key(key)}" if key else
            "No key yet - paste it below (console.anthropic.com)",
            foreground=MUTED if key else WARN)
        self._show_key_entry(not key)

    def save_key(self) -> None:
        key = self.key_entry.get().strip()
        if not key.startswith("sk-"):
            messagebox.showwarning("GameLens", "That does not look like an "
                                   "Anthropic key - they start with sk-ant-.")
            return
        try:
            write_env_key(self.env_path, key)
        except OSError as exc:
            messagebox.showerror("GameLens", f"Could not save the key:\n{exc}")
            return
        self.key_entry.delete(0, "end")
        self._refresh_key_status()
        self._append(self.convo_box, "API key saved. It is used the next time "
                     "you press Start.", "info")

    def restart_as_admin(self) -> None:
        import ctypes

        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe = sys.executable
            params = "-m gamelens.gui"
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, str(app_dir()), 1)
        if int(result) > 32:  # success: the elevated copy is starting
            self.on_close()

    def on_close(self) -> None:
        engine, self.engine = self.engine, None
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
        self.root.destroy()


# -- entry point --------------------------------------------------------------------


def _dispatch_special_modes() -> int | None:
    """The packaged exe is also the Whisper worker and the console commands."""
    if len(sys.argv) > 1 and sys.argv[1] in ("--stt-worker", "--cli"):
        from .stdio import ensure_std_streams

        ensure_std_streams()
    if len(sys.argv) > 1 and sys.argv[1] == "--stt-worker":
        from . import stt_worker

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        return stt_worker.main()
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from .__main__ import main as cli_main

        return cli_main(sys.argv[2:])
    return None


def main() -> int:
    special = _dispatch_special_modes()
    if special is not None:
        return special

    config_path = default_config_path()
    from .__main__ import load_env, setup_debug_log

    setup_debug_log(config_path.parent / "gamelens-debug.log")
    load_env(config_path)

    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    icon = app_dir() / "gamelens.ico"
    if sys.platform == "win32" and icon.exists():
        try:
            root.iconbitmap(str(icon))
        except tk.TclError:
            pass
    GameLensWindow(root, config_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
