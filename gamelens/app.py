"""The engine: capture in the background, answer on a hotkey.

Used two ways. The console command calls run(), which blocks until Ctrl+C.
The window calls start() and stop() and listens to events, so it can show
subtitles, questions, answers and counters as they happen.

Events (kind, data) sent to every listener:
    status    state: starting | ready | listening | transcribing | thinking |
                     speaking | stopped | error
    subtitle  text
    question  text
    answer    text
    usage     the usage snapshot (session and all-time tokens and cost)
    error     message (already phrased for a person to read)
    log       line
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .ask import Asker
from .audio import Recorder, describe_device
from .buffer import DialogueBuffer
from .capture import CaptureWorker
from .config import app_dir
from .hotkeys import admin_note, make_manager
from .ocr import build_engine
from .store import ScreenshotStore
from .stt import Transcriber
from .tts import Speaker
from .usage import UsageTracker

BILLING_URL = "https://console.anthropic.com/settings/billing"


def friendly_error(exc: BaseException) -> str:
    """Turn the errors people actually hit into something they can act on."""
    text = str(exc)
    low = text.lower()
    if "credit balance is too low" in low:
        return ("Your Anthropic API credit has run out. Add credit at "
                f"{BILLING_URL} - it is separate from a Claude subscription.")
    if "invalid x-api-key" in low or "authentication_error" in low:
        return "The API key was rejected. Check the key in the API key box."
    if "no api key" in low:
        return "No API key yet. Paste it into the API key box and press Save."
    if "overloaded" in low:
        return "Claude is overloaded right now. Try the question again in a moment."
    if "connection" in low or "timed out" in low:
        return "Could not reach Claude - check the internet connection."
    return f"{type(exc).__name__}: {text}"


def _beep(frequency: float, duration: float, device: int | None,
          volume: float = 0.22) -> None:
    """A short sine blip, played without disturbing the answer pipeline."""
    try:
        import sounddevice as sd

        rate = 24000
        t = np.linspace(0, duration, int(rate * duration), endpoint=False)
        tone = np.sin(2 * np.pi * frequency * t).astype(np.float32) * volume
        # 5 ms fades, so it does not click.
        fade = max(1, int(rate * 0.005))
        tone[:fade] *= np.linspace(0, 1, fade)
        tone[-fade:] *= np.linspace(1, 0, fade)
        sd.play(tone, samplerate=rate, device=device, blocking=True)
    except Exception:
        pass


class GameLens:
    def __init__(self, config, api_key: str | None = None):
        self.cfg = config
        self.listeners: list[Callable[[str, dict], None]] = []
        self.state = "stopped"
        self.log_path: Path | None = None
        log_file = config.get("log.file")
        if log_file:
            self.log_path = Path(log_file)
            if not self.log_path.is_absolute():
                self.log_path = config.path.parent / self.log_path

        self.notes: list[str] = []
        self.lines_read = 0

        # Where screenshots and usage totals live -------------------------------
        base = config.path.parent if config.path else app_dir()
        shots = config.screenshots
        folder = Path(str(shots.get("folder", "screenshots")))
        self.store = ScreenshotStore(
            folder if folder.is_absolute() else base / folder,
            keep_crops=int(shots.get("keep_subtitle_crops", 100)),
            keep_frames=int(shots.get("keep_game_frames", 10)),
            enabled=bool(shots.get("enabled", True)),
        )
        self.usage = UsageTracker(
            base / "usage.json",
            price_in=float(config.model.get("price_input_per_mtok", 3.0)),
            price_out=float(config.model.get("price_output_per_mtok", 15.0)),
        )

        # Dialogue capture ----------------------------------------------------
        self.ocr, ocr_notes = build_engine(
            list(config.ocr["backends"]), str(config.ocr["language"])
        )
        self.notes.extend(f"ocr: {n}" for n in ocr_notes)
        self.buffer = DialogueBuffer(
            minutes=float(config.buffer["minutes"]),
            dedupe_ratio=float(config.buffer["dedupe_ratio"]),
            min_line_chars=int(config.buffer["min_line_chars"]),
        )
        self.buffer.set_scene_capacity(int(config.capture["keep_scene_frames"]))
        self.capture = CaptureWorker(
            config,
            self.ocr,
            on_text=self._on_subtitle,
            on_clear=self.buffer.mark_gap,
            on_scene=self._on_scene,
            on_error=self._on_capture_error,
            on_crop=self.store.save_crop if self.store.enabled else None,
        )
        self._last_capture_error = ""

        # Voice in and out ----------------------------------------------------
        self.mic_device = config.mic["input_device"]
        self.out_device = config.voice["output_device"]
        self.recorder = Recorder(self.mic_device)
        self.transcriber = Transcriber(
            model_size=str(config.stt["model"]),
            device=str(config.stt["device"]),
            compute_type=str(config.stt["compute_type"]),
            language=str(config.stt["language"]) or None,
            cpu_threads=int(config.stt.get("cpu_threads", 0) or 0),
        )
        self.speaker = Speaker(config, self.out_device)
        self.notes.extend(f"voice: {n}" for n in self.speaker.notes)
        self.beeps = bool(config.voice["beeps"])

        # Claude --------------------------------------------------------------
        self.asker = Asker(config, api_key=api_key)
        self.asker.on_usage = self._on_usage

        self.hotkeys = make_manager(str(config.hotkeys.get("method", "poll")))
        self._busy = threading.Lock()
        self._answering: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False

    # -- events and logging ---------------------------------------------------

    def on(self, listener: Callable[[str, dict], None]) -> None:
        self.listeners.append(listener)

    def emit(self, kind: str, **data) -> None:
        for listener in list(self.listeners):
            try:
                listener(kind, data)
            except Exception:
                pass

    def set_state(self, state: str) -> None:
        self.state = state
        self.emit("status", state=state)

    def log(self, line: str, echo: bool = True) -> None:
        stamped = f"{time.strftime('%H:%M:%S')}  {line}"
        if echo:
            print(stamped, flush=True)
        self.emit("log", line=stamped)
        if self.log_path:
            try:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(stamped + "\n")
            except Exception:
                pass

    def error(self, message: str) -> None:
        self.log(f"!! {message}")
        self.emit("error", message=message)

    # -- capture callbacks ----------------------------------------------------

    def _on_capture_error(self, exc: BaseException) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        if msg != self._last_capture_error:  # say it once, not 3x per second
            self._last_capture_error = msg
            self.error(f"capture problem: {msg}")

    def _on_subtitle(self, text: str, timestamp: float) -> None:
        if self.buffer.add(text, timestamp):
            self.lines_read += 1
            self.log(f"| {text}", echo=False)
            self.emit("subtitle", text=text)

    def _on_scene(self, jpeg: bytes, timestamp: float) -> None:
        if self.cfg.model["attach_screenshot"]:
            self.buffer.add_scene(jpeg, timestamp)
        self.store.save_frame(jpeg, timestamp)

    def _on_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.usage.record(input_tokens, output_tokens)
        self.emit("usage", **self.usage.snapshot())

    def stats(self) -> dict:
        """Counters for the window. Cheap; safe to call several times a second."""
        cap = self.capture
        crops, frames = self.store.counts() if self.store.enabled else (0, 0)
        return {
            "frames_checked": cap.frames_sampled,
            "ocr_runs": cap.ocr_calls,
            "frames_skipped": max(0, cap.frames_sampled - cap.ocr_calls),
            "lines_read": self.lines_read,
            "lines_in_buffer": len(self.buffer),
            "crops_on_disk": crops,
            "frames_on_disk": frames,
            "usage": self.usage.snapshot(),
        }

    # -- the question flow ----------------------------------------------------

    def _start_recording(self) -> None:
        if self.recorder.recording:
            return
        try:
            self.recorder.start()
        except Exception as exc:
            self.error(f"microphone failed: {type(exc).__name__}: {exc}")
            return
        self.speaker.interrupt()  # a new question cancels the previous answer
        self.set_state("listening")
        self.log("** listening ... (speak, then release the key)")
        if self.beeps:
            _beep(880, 0.07, self.out_device)

    def _finish_recording(self) -> None:
        if not self.recorder.recording:
            return
        samples = self.recorder.stop()
        if self.beeps:
            _beep(620, 0.06, self.out_device)
        seconds = len(samples) / 16000.0
        peak = float(abs(samples).max()) if len(samples) else 0.0
        self.log(f"** recorded {seconds:.1f}s, mic peak {peak:.3f} - transcribing ...")
        if len(samples) == 0:
            self.set_state("ready")
            return
        self._dispatch(samples=samples)

    def _toggle(self) -> None:
        if self.recorder.recording:
            self._finish_recording()
        else:
            self._start_recording()

    def _dispatch(self, samples: np.ndarray | None = None,
                  question: str | None = None) -> None:
        if self._answering and self._answering.is_alive():
            self.speaker.interrupt()
        thread = threading.Thread(
            target=self._answer, args=(samples, question),
            name="gamelens-answer", daemon=True
        )
        self._answering = thread
        thread.start()

    def ask(self, question: str) -> None:
        """Ask a typed question - used by the window's text box."""
        question = (question or "").strip()
        if question:
            self._dispatch(question=question)

    def _answer(self, samples: np.ndarray | None, question: str | None) -> None:
        with self._busy:
            try:
                if question is None:
                    self.set_state("transcribing")
                    question = self.transcriber.transcribe(samples)
                    if not question:
                        self.log(".. nothing heard (too quiet, too short, or "
                                 "wrong microphone - see mic peak above)")
                        self.emit("error", message="Nothing heard - speak a bit "
                                  "louder, or check the microphone.")
                        if self.beeps:
                            _beep(330, 0.12, self.out_device)
                        return
                self.log(f">> {question}")
                self.emit("question", text=question)
                self.set_state("thinking")

                transcript = self.buffer.transcript()
                scene = (
                    self.buffer.latest_scene()
                    if self.cfg.model["attach_screenshot"]
                    else None
                )
                collected: list[str] = []

                def sentences():
                    for sentence in self.asker.stream_sentences(
                        transcript, question, scene
                    ):
                        if not collected:
                            self.set_state("speaking")
                        collected.append(sentence)
                        yield sentence

                self.speaker.speak_stream(sentences())
                answer = " ".join(collected)
                self.log(f"<< {answer}")
                self.emit("answer", text=answer)
            except Exception as exc:
                self.error(friendly_error(exc))
                if self.beeps:
                    _beep(220, 0.18, self.out_device)
            finally:
                if not self._stop.is_set():
                    self.set_state("ready")

    def ask_repeat(self) -> None:
        if len(self.buffer) == 0 and not self.buffer.latest_scene():
            self.log(".. nothing captured yet")
            self.emit("error", message="No dialogue captured yet.")
            if self.beeps:
                _beep(330, 0.12, self.out_device)
            return
        self._dispatch(question=str(self.cfg.help["default_question"]))

    def shut_up(self) -> None:
        self.speaker.interrupt()

    # -- lifecycle ------------------------------------------------------------

    def preflight(self) -> list[str]:
        """Resolve model and voice, warm up Whisper. Returns status lines."""
        self.transcriber.preload()
        model = self.asker.resolve_model()
        self.notes.extend(f"model: {n}" for n in self.asker.notes)
        region = self.capture.region
        return [
            f"monitor {self.cfg.capture['monitor']}, subtitle box "
            f"{region.width}x{region.height} at ({region.left},{region.top}), "
            f"{self.cfg.capture['fps']} fps",
            f"ocr       {self.ocr.name}",
            f"speech in {describe_device(self.mic_device, 'microphone')} "
            f"-> whisper {self.cfg.stt['model']}",
            f"speech out {self.speaker.backend_name} "
            f"-> {describe_device(self.out_device, 'output')}",
            f"model     {model}",
            f"buffer    last {self.cfg.buffer['minutes']} minutes of dialogue",
        ]

    def bind_hotkeys(self) -> None:
        keys = self.cfg.hotkeys
        if str(keys.get("mode", "hold")).lower() == "toggle":
            self.hotkeys.bind(str(keys["talk"]), on_press=self._toggle)
        else:
            self.hotkeys.bind(
                str(keys["talk"]),
                on_press=self._start_recording,
                on_release=self._finish_recording,
            )
        if keys.get("repeat"):
            self.hotkeys.bind(str(keys["repeat"]), on_press=self.ask_repeat)
        if keys.get("stop"):
            self.hotkeys.bind(str(keys["stop"]), on_press=self.shut_up)

    def start(self) -> bool:
        """Bring everything up. Blocks for a few seconds; returns success."""
        self.set_state("starting")
        for line in self.preflight():
            self.log(line)
        for note in self.notes:
            self.log(f"note: {note}")
        if self.ocr.name == "none":
            self.error("No OCR engine is working, so no dialogue will be captured.")
        if not self.speaker.available:
            self.error("No speech output is working - answers cannot be spoken.")
        self.log(admin_note())

        # Load Whisper completely before anything else starts.
        self.log("loading speech recognition (a few seconds) ...")
        if self.transcriber.wait_ready(180):
            self.log("speech recognition ready")
        else:
            self.error(f"speech recognition {self.transcriber.status} - "
                       "spoken questions will not work, the repeat key still will")

        self.bind_hotkeys()
        if not self.hotkeys.start():
            self.error(f"Could not grab global hotkeys: {self.hotkeys.error}")
            self.set_state("error")
            return False
        self.capture.start()
        self._started = True
        self.log("capture started")
        self.set_state("ready")
        return True

    def run(self) -> int:
        """Console mode: start, then block until Ctrl+C."""
        if not self.start():
            return 1
        keys = self.cfg.hotkeys
        mode = "Hold" if str(keys.get("mode", "hold")).lower() != "toggle" else "Tap"
        print(
            f"\n  Ready. {mode} {keys['talk']} and ask out loud.\n"
            f"  {keys.get('repeat')} explains the last line without speaking.\n"
            f"  {keys.get('stop')} cuts the answer off.  Ctrl+C here to quit.\n"
        )
        try:
            while not self._stop.is_set():
                self._stop.wait(0.5)
                if not self.capture.is_alive():
                    self.error("the capture thread died - "
                               f"{self.capture.last_error or 'no error recorded'}")
                    return 1
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.speaker.interrupt()
        if self.recorder.recording:
            self.recorder.stop()
        self.capture.stop()
        self.hotkeys.stop()
        self.transcriber.close()
        self.set_state("stopped")
        self.log("stopped")

    stop = shutdown
