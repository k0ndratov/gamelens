"""Text to speech, pipelined so the answer starts playing almost immediately.

Sentences arrive one at a time from the Claude stream. Each is synthesised on a
small thread pool while the previous one is still playing, and a dedicated
playback thread keeps them in order. The practical effect is that speech begins
roughly a second after the question, and never stalls between sentences.

Primary backend is edge-tts: free, no key, genuinely natural voices, needs
internet. Fallback is the Windows SAPI voice via pyttsx3 - robotic, but offline
and always there.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Iterable

from .audio import GameDucker, Player, decode_audio

DEFAULT_VOICE = "en-US-AriaNeural"
_SENTINEL = object()


def list_voices(prefix: str = "en-") -> list[dict]:
    """Available edge-tts voices, filtered by locale prefix."""
    import edge_tts

    voices = asyncio.run(edge_tts.list_voices())
    return [
        {
            "name": v.get("ShortName", ""),
            "gender": v.get("Gender", ""),
            "locale": v.get("Locale", ""),
        }
        for v in voices
        if str(v.get("ShortName", "")).startswith(prefix)
    ]


class EdgeBackend:
    name = "edge-tts"

    def __init__(self, voice: str = DEFAULT_VOICE, rate: str = "+0%",
                 volume: str = "+0%", pitch: str = "+0Hz",
                 sample_rate: int = 24000):
        import edge_tts  # noqa: F401
        import miniaudio  # noqa: F401  (needed to decode the mp3 it returns)

        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.pitch = pitch
        self.sample_rate = sample_rate

    def resolve_voice(self) -> tuple[str, str | None]:
        """Check the configured voice exists; otherwise pick a sane English one."""
        try:
            available = [v["name"] for v in list_voices("en-")]
        except Exception:
            return self.voice, None
        if self.voice in available:
            return self.voice, None
        choice = next(
            (v for v in available if v.startswith("en-US") and v.endswith("Neural")),
            available[0] if available else self.voice,
        )
        note = f"voice '{self.voice}' not found; using '{choice}'"
        self.voice = choice
        return choice, note

    async def _synth_async(self, text: str) -> bytes:
        import edge_tts

        comm = edge_tts.Communicate(
            text, self.voice, rate=self.rate, volume=self.volume, pitch=self.pitch
        )
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buf.extend(chunk["data"])
        return bytes(buf)

    def synth(self, text: str):
        """Return (samples, rate) ready for the player."""
        mp3 = asyncio.run(self._synth_async(text))
        if not mp3:
            return None, self.sample_rate
        return decode_audio(mp3, self.sample_rate)


class SapiBackend:
    """Offline fallback. Plays through the system default device itself."""

    name = "sapi"

    def __init__(self, rate_wpm: int = 185):
        import pyttsx3

        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", rate_wpm)
        self._lock = threading.Lock()

    def speak_blocking(self, text: str) -> None:
        with self._lock:
            self._engine.say(text)
            self._engine.runAndWait()


class Speaker:
    """Speaks a stream of sentences, in order, with overlapping synthesis."""

    def __init__(self, config, output_device: int | None = None):
        voice_cfg = config.get("voice", {}) or {}
        self.notes: list[str] = []
        self.player = Player(output_device)
        self.duck_level = float(voice_cfg.get("duck_game_volume", 0.3))
        self.duck_enabled = self.duck_level < 1.0
        self.backend = None
        self.fallback = None
        self.errors: list[str] = []   # synthesis/playback failures, never hidden
        self.last_spoke = False       # did the last speak_stream play any audio?

        try:
            self.backend = EdgeBackend(
                voice=str(voice_cfg.get("voice", DEFAULT_VOICE)),
                rate=str(voice_cfg.get("rate", "+0%")),
                volume=str(voice_cfg.get("volume", "+0%")),
                pitch=str(voice_cfg.get("pitch", "+0Hz")),
            )
            _chosen, note = self.backend.resolve_voice()
            if note:
                self.notes.append(note)
        except Exception as exc:
            self.notes.append(f"edge-tts unavailable ({type(exc).__name__}: {exc})")

        if self.backend is None:
            try:
                self.fallback = SapiBackend()
                self.notes.append("falling back to the Windows SAPI voice")
            except Exception as exc:
                self.notes.append(f"no speech output available ({exc})")

    @property
    def available(self) -> bool:
        return self.backend is not None or self.fallback is not None

    @property
    def backend_name(self) -> str:
        if self.backend:
            return f"{self.backend.name} ({self.backend.voice})"
        if self.fallback:
            return self.fallback.name
        return "none"

    def interrupt(self) -> None:
        self.player.interrupt()

    # -- the pipeline ---------------------------------------------------------

    def speak_stream(self, sentences: Iterable[str]) -> None:
        if self.backend is None:
            text = " ".join(s for s in sentences if s).strip()
            if text and self.fallback:
                with GameDucker(self.duck_level, self.duck_enabled):
                    self.fallback.speak_blocking(text)
            return

        self.last_spoke = False
        audio_q: "queue.Queue[object]" = queue.Queue()
        ducker_state = {"active": False}
        ducker = GameDucker(self.duck_level, self.duck_enabled)

        def playback() -> None:
            while True:
                item = audio_q.get()
                if item is _SENTINEL:
                    break
                future: Future = item  # type: ignore[assignment]
                try:
                    samples, rate = future.result()
                except Exception as exc:
                    msg = f"speech synthesis failed: {type(exc).__name__}: {exc}"
                    self.errors.append(msg)
                    print(f"  !! {msg}", flush=True)
                    continue
                if samples is None or len(samples) == 0:
                    msg = "speech synthesis returned no audio"
                    self.errors.append(msg)
                    print(f"  !! {msg}", flush=True)
                    continue
                self.last_spoke = True
                if not ducker_state["active"]:
                    ducker.__enter__()
                    ducker_state["active"] = True
                if not self.player.play(samples, rate):
                    break  # interrupted; drop the rest

        worker = threading.Thread(target=playback, name="gamelens-speak", daemon=True)
        worker.start()
        try:
            # Two workers is enough to stay ahead of playback without hammering
            # the service with a whole answer's worth of requests at once.
            with ThreadPoolExecutor(max_workers=2) as pool:
                for sentence in sentences:
                    sentence = (sentence or "").strip()
                    if sentence:
                        audio_q.put(pool.submit(self.backend.synth, sentence))
        finally:
            audio_q.put(_SENTINEL)
            worker.join()
            if ducker_state["active"]:
                ducker.__exit__(None, None, None)

    def say(self, text: str) -> None:
        """Speak a single short string - used for status chirps."""
        self.speak_stream([text])
