"""Logic tests that need no screen, microphone, network or API key.

Run with:  python -m tests.test_core      (from the project root)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelens import capture, ocr  # noqa: E402
from gamelens.ask import split_sentences  # noqa: E402
from gamelens.buffer import DialogueBuffer, similarity  # noqa: E402
from gamelens.capture import Box, resolve_region  # noqa: E402
from gamelens.config import Config  # noqa: E402
from gamelens.hotkeys import Combo, base_key_name  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


# -- region maths ------------------------------------------------------------


def test_region() -> None:
    mon = Box(0, 0, 1920, 1080)
    box = resolve_region(mon, {"left": 0.15, "top": 0.78, "width": 0.70,
                               "height": 0.16})
    check("region converts fractions to pixels",
          (box.left, box.top, box.width, box.height) == (288, 842, 1344, 173),
          str(box))

    # Second monitor to the right, with an offset origin.
    mon2 = Box(1920, 0, 2560, 1440)
    box2 = resolve_region(mon2, {"left": 0.1, "top": 0.8, "width": 0.8,
                                 "height": 0.15})
    check("region respects monitor offset", box2.left == 1920 + 256, str(box2))

    # A region running off the edge must be clamped, not crash the grab.
    box3 = resolve_region(mon, {"left": 0.9, "top": 0.95, "width": 0.5,
                                "height": 0.5})
    check("region is clamped to the monitor",
          box3.left + box3.width <= 1920 and box3.top + box3.height <= 1080,
          str(box3))


# -- change detection --------------------------------------------------------


def synthetic_frame(text_rows: list[int], noise_seed: int,
                    width: int = 200, height: int = 60) -> np.ndarray:
    """A fake BGRA capture: dark moving background plus bright 'text' rows."""
    rng = np.random.default_rng(noise_seed)
    frame = rng.integers(0, 120, size=(height, width, 4), dtype=np.uint8)
    frame[:, :, 3] = 255
    for row in text_rows:
        frame[row:row + 4, 20:180, :3] = 240
    return frame


def test_signature() -> None:
    a = synthetic_frame([20, 30], noise_seed=1)
    b = synthetic_frame([20, 30], noise_seed=99)  # same text, different scene
    c = synthetic_frame([20, 40], noise_seed=1)   # different text

    def sig(frame):
        gray = capture.to_gray(frame)
        return capture.mask_signature(capture.text_mask(gray, 170))

    check("same text over a different background hashes the same",
          sig(a) == sig(b))
    check("different text hashes differently", sig(a) != sig(c))

    empty = synthetic_frame([], noise_seed=5)
    gray = capture.to_gray(empty)
    fraction = float(capture.text_mask(gray, 170).mean())
    check("an empty subtitle area reads as empty", fraction < 0.002,
          f"fraction={fraction:.5f}")

    gray_a = capture.to_gray(a)
    fraction_a = float(capture.text_mask(gray_a, 170).mean())
    check("text present is above the threshold", fraction_a > 0.002,
          f"fraction={fraction_a:.5f}")


def test_prepare_for_ocr() -> None:
    frame = synthetic_frame([20, 30], noise_seed=3)
    gray = capture.to_gray(frame)
    mask = capture.text_mask(gray, 170)
    img = capture.prepare_for_ocr(gray, mask, scale=2)
    arr = np.asarray(img)
    check("prepared crop is upscaled", img.size == (400, 120), str(img.size))
    check("prepared crop is dark text on white",
          arr.max() == 255 and arr.min() < 100, f"min={arr.min()} max={arr.max()}")

    jpeg = capture.encode_jpeg(frame, target_width=100)
    check("jpeg encoding produces a real jpeg",
          jpeg.startswith(b"\xff\xd8") and len(jpeg) > 100, str(len(jpeg)))


# -- the dialogue buffer -----------------------------------------------------


def test_buffer_dedupe() -> None:
    buf = DialogueBuffer(minutes=5, dedupe_ratio=0.88, min_line_chars=3)
    now = time.time()

    check("a new line is accepted",
          buf.add("Winter is coming to Velen.", now) is True)
    check("the identical line is not duplicated",
          buf.add("Winter is coming to Velen.", now + 0.3) is False)
    check("a one-character OCR wobble is not duplicated",
          buf.add("Winter is corning to Velen.", now + 0.6) is False)
    check("a wobbled re-read does not overwrite a good read",
          buf.last_text() == "Winter is coming to Velen.", buf.last_text())
    check("a line still being revealed is absorbed",
          buf.add("Winter is coming to Velen, witcher.", now + 0.9) is False)
    check("the fuller version of a revealed line wins",
          buf.last_text() == "Winter is coming to Velen, witcher.",
          buf.last_text())
    check("a genuinely different line is accepted",
          buf.add("Then you'd best be on your way.", now + 2) is True)
    check("short noise is rejected", buf.add("a", now + 3) is False)
    check("buffer holds two lines", len(buf) == 2, str(len(buf)))


def test_buffer_window() -> None:
    buf = DialogueBuffer(minutes=1, dedupe_ratio=0.88, min_line_chars=3)
    now = time.time()
    buf.add("Ancient history, this one.", now - 120)
    buf.add("Spoken a moment ago.", now - 5)
    lines = buf.lines()
    check("lines older than the window are dropped",
          len(lines) == 1 and lines[0].text == "Spoken a moment ago.",
          str([l.text for l in lines]))


def test_transcript_format() -> None:
    buf = DialogueBuffer(minutes=5)
    now = time.time()
    buf.add("First thing said.", now - 75)
    buf.add("Second thing said.", now - 4)
    text = buf.transcript()
    check("transcript is newest last",
          text.index("First") < text.index("Second"))
    check("transcript carries relative timestamps",
          "[-1:15]" in text and "[-0:04]" in text, text)


def test_similarity() -> None:
    check("similarity catches near-identical lines",
          similarity("Geralt of Rivia", "Geralt of Rivla") > 0.9)
    check("similarity separates different lines",
          similarity("Geralt of Rivia", "Where is Ciri?") < 0.5)


# -- sentence splitting for the TTS pipeline ---------------------------------


def test_split_sentences() -> None:
    done, rest = split_sentences("He said he found the girl. Now he wants pay")
    check("a complete sentence is emitted early",
          done == ["He said he found the girl."], str(done))
    check("the incomplete tail is held back", rest.strip() == "Now he wants pay",
          repr(rest))

    done, rest = split_sentences("Wait")
    check("no sentence is emitted without punctuation", done == [], str(done))

    done, rest = split_sentences("Is it done? Yes! Good.")
    check("several sentences split at once", len(done) == 3, str(done))

    done, rest = split_sentences("Dr. Hoffman asked about the contract. He left.")
    check("abbreviations do not split a sentence",
          done and done[0].startswith("Dr. Hoffman"), str(done))


# -- OCR text cleanup --------------------------------------------------------


def test_incremental_streaming() -> None:
    """Deltas arriving a few characters at a time must lose nothing."""
    answer = (
        "He is telling you the contract is off. The ealdorman never paid him, "
        "so he left the village. Follow him to the inn if you want the reward!"
    )
    deltas = [answer[i:i + 7] for i in range(0, len(answer), 7)]

    pending, spoken = "", []
    for delta in deltas:
        pending += delta
        sentences, pending = split_sentences(pending)
        spoken.extend(sentences)
    if pending.strip():
        spoken.append(pending.strip())

    check("streaming yields three sentences", len(spoken) == 3, str(spoken))
    check("streaming loses no characters",
          " ".join(spoken) == answer, " ".join(spoken))
    check("the first sentence is ready before the answer ends",
          spoken[0].endswith("is off."), spoken[0])


def test_stt_worker() -> None:
    """The Whisper worker process: protocol, crash on load, death mid-request."""
    from gamelens.stt import Transcriber

    tone = (np.sin(np.linspace(0, 200, 16000)) * 0.3).astype(np.float32)

    t = Transcriber("__fake__")
    ready = t.wait_ready(30)
    check("worker starts and reports ready", ready, t.status)
    out = t.transcribe(tone)
    check("worker round trip returns text", out == "fake heard 16000 samples", out)
    out2 = t.transcribe(tone[:8000] * 1.5)
    check("a second request on the same worker works",
          out2 == "fake heard 8000 samples", out2)
    check("silence is rejected without a round trip",
          t.transcribe(np.zeros(16000, dtype=np.float32)) == "")
    check("a mis-press is rejected without a round trip",
          t.transcribe(tone[:1000]) == "")
    t.close()
    check("close() stops the worker", t._proc is None)

    crash = Transcriber("__fake_crash__")
    check("a worker that dies on load is reported, not hung",
          crash.wait_ready(30) is False)
    check("the reason names the exit code", "code 3" in crash.status, crash.status)

    die = Transcriber("__fake_die__")
    die.wait_ready(30)
    try:
        die.transcribe(tone)
        check("a worker dying mid-request raises", False, "no exception")
    except RuntimeError as exc:
        check("a worker dying mid-request raises a clear error",
              "worker" in str(exc), str(exc))
    die.close()


def test_clean_text() -> None:
    check("newlines collapse to one line",
          ocr.clean_text("Winter is\ncoming  to Velen") ==
          "Winter is coming to Velen")
    check("pipe artefacts become I", ocr.clean_text("| know him.") == "I know him.")
    check("spaces before punctuation are fixed",
          ocr.clean_text("Really ? Yes .") == "Really? Yes.")
    check("single-character noise is discarded", ocr.clean_text(".") == "")
    check("empty input is safe", ocr.clean_text("") == "")


# -- config ------------------------------------------------------------------


def test_config() -> None:
    cfg = Config.load(Path(__file__).resolve().parent.parent / "config.yaml")
    check("config loads", isinstance(cfg.data, dict))
    check("dotted get works", cfg.get("capture.fps") == 3.0, str(cfg.get("capture.fps")))
    check("missing keys fall back", cfg.get("nope.nothing", "x") == "x")
    cfg.set("capture.region.left", 0.2)
    check("dotted set works", cfg.capture["region"]["left"] == 0.2)
    check("defaults fill in absent sections", "beeps" in cfg.voice)


# -- hotkey parsing ----------------------------------------------------------


def test_combo() -> None:
    combo = Combo("<ctrl>+<alt>+q")
    check("combo parses modifiers", combo.modifiers == {"ctrl", "alt"},
          str(combo.modifiers))
    check("combo parses the main key", combo.main == "q", combo.main)

    plain = Combo("<f10>")
    check("a bare function key parses", plain.main == "f10" and not plain.modifiers,
          f"{plain.main} {plain.modifiers}")

    win = Combo("<win>+<shift>+Z")
    check("aliases and case are handled",
          win.modifiers == {"cmd", "shift"} and win.main == "z",
          f"{win.modifiers} {win.main}")

    # pynput reports ctrl_l / shift_r etc; these must normalise to the modifier
    # name without mangling the rest of the word.
    cases = {
        "ctrl_l": "ctrl", "ctrl_r": "ctrl", "shift_l": "shift",
        "shift_r": "shift", "alt_l": "alt", "alt_gr": "alt_gr",
        "cmd": "cmd", "f10": "f10", "space": "space",
    }
    bad = {raw: base_key_name(raw) for raw, want in cases.items()
           if base_key_name(raw) != want}
    check("left/right key suffixes normalise correctly", not bad, str(bad))
    check("normalised modifiers map to a modifier name",
          all(base_key_name(k) in ("ctrl", "shift", "alt", "cmd", "alt_gr")
              for k in ("ctrl_l", "shift_r", "alt_l", "cmd")))


# -- the capture loop, end to end with fakes ----------------------------------


class FakeOcr:
    """Returns a scripted line per distinct frame signature."""

    name = "fake"

    def __init__(self, script: dict[str, str]):
        self.script = script
        self.calls = 0

    def read(self, image) -> str:
        self.calls += 1
        # Index by how many distinct frames we have been shown.
        return self.script.get(str(self.calls), "")


def test_capture_pipeline_dedupe() -> None:
    """A line spanning many frames must cost exactly one OCR call."""
    buf = DialogueBuffer(minutes=5)
    engine = FakeOcr({"1": "Winter is coming to Velen.",
                      "2": "Then you'd best be on your way."})

    frames = (
        [synthetic_frame([20, 30], seed) for seed in range(12)]       # line one
        + [synthetic_frame([], 100)]                                   # gap
        + [synthetic_frame([20, 40], seed) for seed in range(200, 208)]  # line two
    )

    last_sig = None
    for frame in frames:
        gray = capture.to_gray(frame)
        mask = capture.text_mask(gray, 170)
        if float(mask.mean()) < 0.002:
            buf.mark_gap()
            last_sig = None
            continue
        sig = capture.mask_signature(mask)
        if sig == last_sig:
            continue
        last_sig = sig
        text = engine.read(capture.prepare_for_ocr(gray, mask))
        if text:
            buf.add(text)

    check("21 frames cost only 2 OCR calls", engine.calls == 2, str(engine.calls))
    check("both lines landed in the buffer", len(buf) == 2, str(len(buf)))
    check("the transcript reads in order",
          buf.transcript().index("Winter") < buf.transcript().index("best"))


def main() -> int:
    print("GameLens logic tests\n")
    for group in (
        test_region, test_signature, test_prepare_for_ocr, test_buffer_dedupe,
        test_buffer_window, test_transcript_format, test_similarity,
        test_split_sentences, test_incremental_streaming, test_stt_worker,
        test_clean_text,
        test_config, test_combo,
        test_capture_pipeline_dedupe,
    ):
        print(f"{group.__name__}:")
        group()
        print()
    print(f"{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
