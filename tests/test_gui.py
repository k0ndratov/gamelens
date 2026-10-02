"""Tests for the window and its helpers. Needs tkinter and a display
(run under xvfb-run on Linux).  Run with:  python tests/test_gui.py"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from gamelens import gui  # noqa: E402
from gamelens.app import friendly_error  # noqa: E402
from gamelens.store import ScreenshotStore  # noqa: E402
from gamelens.usage import UsageTracker  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def test_usage(tmp: Path) -> None:
    path = tmp / "usage.json"
    u = UsageTracker(path, price_in=3.0, price_out=15.0)
    u.record(1_000_000, 0)
    u.record(0, 100_000)
    snap = u.snapshot()
    check("usage counts questions", snap["session"]["questions"] == 2)
    check("cost = 1M in at $3 + 100k out at $15 = $4.50",
          abs(snap["session"]["cost"] - 4.5) < 1e-9, snap["session"]["cost"])
    again = UsageTracker(path)
    check("all-time totals survive a restart",
          again.snapshot()["total"]["questions"] == 2
          and again.snapshot()["session"]["questions"] == 0)
    (tmp / "bad.json").write_text("{not json")
    check("a corrupt usage file is ignored, not fatal",
          UsageTracker(tmp / "bad.json").snapshot()["total"]["questions"] == 0)


def test_store(tmp: Path) -> None:
    store = ScreenshotStore(tmp / "shots", keep_crops=3, keep_frames=2)
    img = Image.new("RGB", (120, 20), "black")
    for i in range(6):
        store.save_crop(img, f"Line number {i}! Witcher?", time.time() + i)
        time.sleep(0.01)
    for i in range(5):
        store.save_frame(b"\xff\xd8fakejpeg", time.time() + i)
        time.sleep(0.01)
    crops, frames = store.counts()
    check("subtitle crops are pruned to the limit", crops == 3, crops)
    check("game frames are pruned to the limit", frames == 2, frames)
    names = sorted(p.name for p in (tmp / "shots" / "subtitles").iterdir())
    check("the newest crops are the ones kept",
          all("line-number-" in n for n in names) and any("-5" in n for n in names),
          names)
    check("file names carry readable text", names[0].endswith(".png"), names)
    off = ScreenshotStore(tmp / "off", enabled=False)
    check("disabled store writes nothing", off.save_crop(img, "x") is None
          and not (tmp / "off").exists())


def test_env(tmp: Path) -> None:
    env = tmp / ".env"
    env.write_text("# comment\nOTHER=1\nANTHROPIC_API_KEY=sk-ant-your-key-here\n")
    check("the placeholder key counts as no key", gui.read_env_key(env) == "")
    gui.write_env_key(env, "sk-ant-api03-REALKEY1234")
    text = env.read_text()
    check("saving replaces the key line", text.count("ANTHROPIC_API_KEY=") == 1
          and "REALKEY1234" in text, text)
    check("other lines are kept", "OTHER=1" in text and "# comment" in text)
    check("reading it back works", gui.read_env_key(env) == "sk-ant-api03-REALKEY1234")
    check("the key is masked for display",
          gui.mask_key("sk-ant-api03-REALKEY1234") == "sk-ant-…1234")


def test_names_and_errors() -> None:
    check("hotkey names read naturally",
          gui.pretty_combo("<ctrl>+<alt>+q") == "Ctrl+Alt+Q",
          gui.pretty_combo("<ctrl>+<alt>+q"))
    check("mouse buttons read naturally", gui.pretty_combo("<mouse5>") == "Mouse 5")
    check("function keys read naturally", gui.pretty_combo("<f10>") == "F10")
    low = friendly_error(RuntimeError("Error code: 400 - Your credit balance is "
                                      "too low to access the Anthropic API."))
    check("the credit error explains what to do",
          "credit" in low and "console.anthropic.com" in low, low)
    nokey = friendly_error(RuntimeError("No API key. Set the ..."))
    check("the missing-key error points at the key box", "API key box" in nokey)


def test_window(tmp: Path) -> None:
    import tkinter as tk

    cfg = tmp / "config.yaml"
    cfg.write_text(Path(__file__).resolve().parent.parent.joinpath(
        "config.yaml").read_text())
    root = tk.Tk()
    w = gui.GameLensWindow(root, cfg)

    class Fake:
        _started = True
        def stats(self):
            return {"frames_checked": 100, "ocr_runs": 4, "frames_skipped": 96,
                    "lines_read": 4, "lines_in_buffer": 4, "crops_on_disk": 4,
                    "frames_on_disk": 2,
                    "usage": {"session": {"questions": 1, "input": 2000,
                                          "output": 80, "cost": 0.0072},
                              "total": {"questions": 1, "input": 2000,
                                        "output": 80, "cost": 0.0072}}}
        def stop(self):
            pass
        def ask(self, q):
            self.asked = q

    errors = []
    root.report_callback_exception = lambda *a: errors.append(a)
    for kind, data in [
        ("status", {"state": "starting"}), ("status", {"state": "ready"}),
        ("subtitle", {"text": "Hello there"}), ("question", {"text": "Q?"}),
        ("answer", {"text": "A."}), ("error", {"message": "bad"}),
        ("usage", Fake().stats()["usage"]), ("log", {"line": "x"}),
        ("_started", {"ok": True}), ("_calibrated", {"message": "saved"}),
        ("status", {"state": "listening"}), ("status", {"state": "speaking"}),
        ("_stopped", {}), ("status", {"state": "made-up-state"}),
    ]:
        w._handle(kind, data)
    root.update()
    check("every event kind is handled without an error", not errors, errors)
    check("with no engine the button offers Start, whatever the status",
          "Start" in str(w.start_btn.cget("text")), w.start_btn.cget("text"))
    w.engine = Fake()
    w._set_status("ready")
    check("with an engine running the button offers Stop",
          "Stop" in str(w.start_btn.cget("text")), w.start_btn.cget("text"))

    w.engine = Fake()
    w._refresh_counters()
    check("counters show the duplicate-skip share",
          w.cap_vals["saved"].cget("text") == "96 (96%)", w.cap_vals["saved"].cget("text"))
    check("cost is shown", w.cost_session.cget("text") == "$0.007",
          w.cost_session.cget("text"))

    w.ask_entry.delete(0, "end")
    w.ask_entry.insert(0, "What is a ealdorman?")
    w.ask_typed()
    check("a typed question reaches the engine",
          getattr(w.engine, "asked", None) == "What is a ealdorman?")

    w.engine = None
    w.ask_entry.insert(0, "hello")
    w.ask_typed()
    convo = w.convo_box.get("1.0", "end")
    check("asking before Start explains itself", "Press Start first" in convo)

    w.key_entry.insert(0, "sk-ant-api03-ABCDEFGHIJKLMN")
    w.save_key()
    check("saving a key from the window writes .env",
          gui.read_env_key(tmp / ".env") == "sk-ant-api03-ABCDEFGHIJKLMN")
    check("after saving, the key shows masked",
          "…KLMN" in w.key_status.cget("text"), w.key_status.cget("text"))
    root.destroy()


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for t in (test_usage, test_store, test_env):
            print(f"{t.__name__}:")
            sub = tmp / t.__name__
            sub.mkdir()
            t(sub)
            print()
        print("test_names_and_errors:")
        test_names_and_errors()
        print("\ntest_window:")
        sub = tmp / "win"
        sub.mkdir()
        test_window(sub)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
