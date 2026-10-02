"""Global hotkeys that work while a fullscreen game has focus.

pynput's own GlobalHotKeys only reports activation, but push-to-talk needs both
press and release, so combos are tracked by hand here. A combo is written like
"<ctrl>+<alt>+q" or just "<f10>"; modifiers are matched left-or-right.
"""

from __future__ import annotations

import threading
from typing import Callable

_MOD_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "alt_gr": "alt",
    "altgr": "alt",
    "shift": "shift",
    "cmd": "cmd",
    "win": "cmd",
    "super": "cmd",
}


def _normalise_token(token: str) -> str:
    return token.strip().strip("<>").lower()


def base_key_name(raw: str) -> str:
    """Drop pynput's left/right suffix: ctrl_l -> ctrl, shift_r -> shift.

    Written as an explicit suffix check rather than str.rstrip, which would eat
    the wrong characters ("ctrl_l".rstrip("_lr") gives "ct").
    """
    name = raw.lower()
    if len(name) > 2 and name[-2:] in ("_l", "_r"):
        return name[:-2]
    return name


class Combo:
    def __init__(self, spec: str):
        tokens = [_normalise_token(t) for t in spec.split("+") if t.strip()]
        if not tokens:
            raise ValueError(f"empty hotkey spec: {spec!r}")
        self.spec = spec
        self.modifiers: set[str] = set()
        main: str | None = None
        for token in tokens:
            if token in _MOD_ALIASES:
                self.modifiers.add(_MOD_ALIASES[token])
            else:
                main = token
        if main is None:
            # A pure-modifier hotkey; use the last modifier as the trigger.
            main = tokens[-1]
            self.modifiers.discard(_MOD_ALIASES.get(main, main))
        self.main = main

    def __repr__(self) -> str:
        return f"Combo({self.spec!r})"


class HotkeyManager:
    """Binds combos to press/release callbacks on a background listener."""

    def __init__(self):
        self._bindings: list[tuple[Combo, Callable[[], None] | None,
                                   Callable[[], None] | None]] = []
        self._held_mods: set[str] = set()
        self._active: set[str] = set()
        self._listener = None
        self._lock = threading.Lock()
        self.error: str | None = None
        self.trace: Callable[[str], None] | None = None  # debug sink

    def bind(self, spec: str, on_press: Callable[[], None] | None = None,
             on_release: Callable[[], None] | None = None) -> None:
        self._bindings.append((Combo(spec), on_press, on_release))

    # -- key identification ---------------------------------------------------

    def _key_names(self, key) -> tuple[str | None, str | None]:
        """Return (modifier_name, key_name) for a pynput key.

        Letters and digits are identified by their Windows virtual-key code
        rather than by the character, because with Ctrl held Windows reports
        Ctrl+Q as the control character \\x11, not "q".
        """
        from pynput import keyboard

        if isinstance(key, keyboard.Key):
            base = base_key_name(key.name)
            return _MOD_ALIASES.get(base), base

        vk = getattr(key, "vk", None)
        if vk is not None and (0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A):
            return None, chr(vk).lower()
        char = getattr(key, "char", None)
        if char and ord(char) >= 32:
            return None, char.lower()
        if vk is not None:
            return None, f"vk{vk}"
        return None, None

    def _trace(self, what: str, key, mod, name) -> None:
        if self.trace is None:
            return
        with self._lock:
            held = ",".join(sorted(self._held_mods)) or "-"
        self.trace(f"{what:<7} raw={key!r:<22} -> key={name!r:<8} "
                   f"mod={mod!r:<7} held_mods={held}")

    # -- listener callbacks ---------------------------------------------------

    def _on_press(self, key) -> None:
        mod, name = self._key_names(key)
        self._trace("press", key, mod, name)
        with self._lock:
            if mod:
                self._held_mods.add(mod)
            if not name:
                return
            for combo, on_press, _ in self._bindings:
                if combo.main != name or combo.spec in self._active:
                    continue
                if combo.modifiers <= self._held_mods:
                    self._active.add(combo.spec)
                    callback = on_press
                    break
            else:
                return
        if callback:
            threading.Thread(target=_safe(callback), daemon=True).start()

    def _on_release(self, key) -> None:
        mod, name = self._key_names(key)
        self._trace("release", key, mod, name)
        fired: list[Callable[[], None]] = []
        with self._lock:
            if mod:
                self._held_mods.discard(mod)
            for combo, _, on_release in self._bindings:
                if combo.spec not in self._active:
                    continue
                # Releasing the main key, or any required modifier, ends it.
                if name == combo.main or (mod and mod in combo.modifiers):
                    self._active.discard(combo.spec)
                    if on_release:
                        fired.append(on_release)
        for callback in fired:
            threading.Thread(target=_safe(callback), daemon=True).start()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        try:
            from pynput import keyboard

            self._listener = keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release
            )
            self._listener.start()
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


def _safe(callback: Callable[[], None]) -> Callable[[], None]:
    def wrapped() -> None:
        try:
            callback()
        except Exception as exc:  # a bad handler must not kill the listener
            print(f"[gamelens] hotkey handler failed: {type(exc).__name__}: {exc}")

    return wrapped


# ---------------------------------------------------------------------------
# Polling method: reads key state directly instead of installing a hook.
#
# A keyboard hook is not delivered keystrokes that are headed for a window with
# higher privileges, and many games (or their launchers) run that way. Asking
# Windows "is this key down right now?" a hundred times a second has no such
# restriction, and also lets a mouse button act as push-to-talk.
# ---------------------------------------------------------------------------

_VK_NAMES = {
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "cmd": 0x5B,
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B,
    "backspace": 0x08, "pause": 0x13, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "caps_lock": 0x14, "print_screen": 0x2C, "scroll_lock": 0x91,
    "num_lock": 0x90,
    # Mouse buttons: usable as a push-to-talk key.
    "mouse1": 0x01, "mouse2": 0x02, "mouse3": 0x04,
    "mouse4": 0x05, "mouse5": 0x06,
    "xbutton1": 0x05, "xbutton2": 0x06,
}


def vk_for(name: str) -> int:
    """Windows virtual-key code for a hotkey token such as 'q', 'f9', 'mouse5'."""
    name = name.lower()
    if name in _VK_NAMES:
        return _VK_NAMES[name]
    if len(name) == 1 and (name.isalpha() or name.isdigit()):
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    if name.startswith("num") and name[3:].isdigit() and len(name) == 4:
        return 0x60 + int(name[3:])
    raise ValueError(f"don't know how to poll the key {name!r}")


def _default_key_reader() -> Callable[[int], bool]:
    import ctypes

    get_state = ctypes.windll.user32.GetAsyncKeyState
    get_state.argtypes = [ctypes.c_int]
    get_state.restype = ctypes.c_short

    def is_down(vk: int) -> bool:
        return bool(get_state(vk) & 0x8000)

    return is_down


class PollHotkeys:
    """Same interface as HotkeyManager, driven by polling GetAsyncKeyState."""

    interval = 0.008  # ~120 Hz; a keypress is never shorter than this

    def __init__(self, reader: Callable[[int], bool] | None = None):
        self._reader = reader
        self._bindings: list[dict] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None
        self.trace: Callable[[str], None] | None = None

    def bind(self, spec: str, on_press: Callable[[], None] | None = None,
             on_release: Callable[[], None] | None = None) -> None:
        combo = Combo(spec)
        mod_vks = [_VK_NAMES[m] for m in sorted(combo.modifiers)]
        self._bindings.append({
            "combo": combo,
            "mods": mod_vks,
            "main": vk_for(combo.main),
            "on_press": on_press,
            "on_release": on_release,
            "down": False,
        })

    def start(self) -> bool:
        try:
            if self._reader is None:
                self._reader = _default_key_reader()
            # Fail now, with a clear message, rather than inside the thread.
            self._reader(0x41)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gamelens-poll",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        watched = sorted({b["main"] for b in self._bindings}
                         | {m for b in self._bindings for m in b["mods"]})
        previous: dict[int, bool] = {}
        while not self._stop.is_set():
            try:
                state = {vk: self._reader(vk) for vk in watched}
                if self.trace:
                    for vk, down in state.items():
                        if previous.get(vk) is not None and previous[vk] != down:
                            self.trace(f"{'press  ' if down else 'release'} "
                                       f"vk=0x{vk:02X}")
                previous = state

                for b in self._bindings:
                    now = state[b["main"]] and all(state[m] for m in b["mods"])
                    if now and not b["down"]:
                        b["down"] = True
                        if b["on_press"]:
                            threading.Thread(target=_safe(b["on_press"]),
                                             daemon=True).start()
                    elif not now and b["down"]:
                        b["down"] = False
                        if b["on_release"]:
                            threading.Thread(target=_safe(b["on_release"]),
                                             daemon=True).start()
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.interval)


def make_manager(method: str = "poll"):
    """Pick the hotkey backend. Polling is Windows-only; elsewhere use the hook."""
    import sys

    if str(method).lower() == "poll" and sys.platform == "win32":
        return PollHotkeys()
    return HotkeyManager()


def is_admin() -> bool | None:
    """True/False on Windows, None if it cannot be determined."""
    import sys

    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def admin_note() -> str:
    state = is_admin()
    if state is None:
        return "administrator: unknown"
    if state:
        return "administrator: YES (can see keys inside elevated games)"
    return ("administrator: NO (cannot see keys while an elevated game has "
            "focus - start PowerShell with 'Run as administrator')")
