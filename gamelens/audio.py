"""Microphone capture, playback, and keeping the game from drowning the answer."""

from __future__ import annotations

import queue
import threading

import numpy as np

MIC_RATE = 16000  # what Whisper wants


def _sd():
    import sounddevice as sd

    return sd


def list_devices() -> list[dict]:
    sd = _sd()
    out = []
    for index, dev in enumerate(sd.query_devices()):
        out.append(
            {
                "index": index,
                "name": dev["name"],
                "inputs": dev["max_input_channels"],
                "outputs": dev["max_output_channels"],
                "default_rate": int(dev["default_samplerate"]),
            }
        )
    return out


def default_devices() -> tuple[int | None, int | None]:
    sd = _sd()
    try:
        inp, outp = sd.default.device
        return inp, outp
    except Exception:
        return None, None


def describe_device(index: int | None, kind: str) -> str:
    if index is None:
        return f"default {kind}"
    try:
        sd = _sd()
        return str(sd.query_devices(index)["name"])
    except Exception:
        return f"device {index}"


class Recorder:
    """Push-to-talk microphone capture.

    start() opens the stream and keeps collecting until stop(), which returns
    the audio as mono float32 at 16 kHz. Short bursts are expected - a question
    is two or three seconds.
    """

    def __init__(self, device: int | None = None, rate: int = MIC_RATE):
        self.device = device
        self.rate = rate
        self._chunks: queue.Queue[np.ndarray] = queue.Queue()
        self._stream = None
        self._lock = threading.Lock()
        self.peak = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, _frames, _time, status):  # pragma: no cover
        if status:
            pass  # overruns are not worth interrupting the player for
        block = indata.copy().reshape(-1)
        self.peak = max(self.peak, float(np.abs(block).max(initial=0.0)))
        self._chunks.put(block)

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            sd = _sd()
            while not self._chunks.empty():
                self._chunks.get_nowait()
            self.peak = 0.0
            self._stream = sd.InputStream(
                samplerate=self.rate,
                channels=1,
                dtype="float32",
                device=self.device,
                blocksize=1024,
                callback=self._callback,
            )
            self._stream.start()

    def stop(self) -> np.ndarray:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        blocks: list[np.ndarray] = []
        while not self._chunks.empty():
            blocks.append(self._chunks.get_nowait())
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(blocks).astype(np.float32)


class Player:
    """Plays float32 mono audio to one output device, interruptibly."""

    def __init__(self, device: int | None = None):
        self.device = device
        self._interrupt = threading.Event()
        self._lock = threading.Lock()

    def interrupt(self) -> None:
        self._interrupt.set()

    def play(self, samples: np.ndarray, rate: int) -> bool:
        """Blocks until finished. Returns False if it was interrupted."""
        if samples is None or len(samples) == 0:
            return True
        sd = _sd()
        with self._lock:
            self._interrupt.clear()
            try:
                sd.play(samples, samplerate=rate, device=self.device)
            except Exception:
                # A bad explicit device should not kill the answer - retry default.
                sd.play(samples, samplerate=rate)
            # Poll so an interrupt can cut playback mid-sentence. The expected
            # duration is the authority; the stream's active flag is only used
            # as an early exit, since not every backend reports it reliably.
            import time

            duration = len(samples) / float(rate)
            deadline = time.monotonic() + duration + 0.15
            while time.monotonic() < deadline:
                if self._interrupt.is_set():
                    sd.stop()
                    return False
                try:
                    stream = sd.get_stream()
                    if stream is not None and not stream.active:
                        break
                except Exception:
                    pass
                self._interrupt.wait(0.03)
            return True


def decode_audio(data: bytes, rate: int = 24000) -> tuple[np.ndarray, int]:
    """Decode MP3/WAV bytes to mono float32 at `rate`."""
    import miniaudio

    decoded = miniaudio.decode(
        data,
        output_format=miniaudio.SampleFormat.FLOAT32,
        nchannels=1,
        sample_rate=rate,
    )
    samples = np.frombuffer(memoryview(decoded.samples), dtype=np.float32)
    return samples.copy(), int(decoded.sample_rate)


class GameDucker:
    """Temporarily lowers every other app's volume so the answer is audible.

    Windows only, and entirely optional - if pycaw is missing this does nothing.
    """

    def __init__(self, level: float = 0.3, enabled: bool = True):
        self.level = max(0.0, min(1.0, float(level)))
        self.enabled = bool(enabled)
        self._saved: list[tuple[object, float]] = []
        self._available = False
        if self.enabled:
            try:
                import comtypes  # noqa: F401
                from pycaw.pycaw import AudioUtilities  # noqa: F401

                self._available = True
            except Exception:
                self._available = False

    def __enter__(self) -> "GameDucker":
        if not (self.enabled and self._available):
            return self
        try:
            import os

            import comtypes
            from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

            comtypes.CoInitialize()
            own_pid = os.getpid()
            for session in AudioUtilities.GetAllSessions():
                if not session.Process or session.Process.pid == own_pid:
                    continue
                volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                current = volume.GetMasterVolume()
                if current > self.level:
                    self._saved.append((volume, current))
                    volume.SetMasterVolume(self.level, None)
        except Exception:
            self._saved = []
        return self

    def __exit__(self, *_exc) -> None:
        for volume, previous in self._saved:
            try:
                volume.SetMasterVolume(previous, None)
            except Exception:
                pass
        self._saved = []
        try:
            import comtypes

            comtypes.CoUninitialize()
        except Exception:
            pass
