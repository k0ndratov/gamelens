"""Speech to text for the player's question, running locally.

The model runs in a separate worker process (see stt_worker.py), because loading
CTranslate2 into the same process as the Windows OCR crashes it. The worker is
started in the background at launch so the first question is not slower than
the rest, and is restarted once if it dies.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

from .audio import MIC_RATE

# Whisper happily hallucinates these on silence or breath noise.
_HALLUCINATIONS = {
    "", "thank you.", "thanks for watching!", "thank you for watching.",
    "you", "bye.", "[music]", "(music)", "um", "uh", ".", "..", "...",
    "subs by www.zeoranger.co.uk", "subtitles by the amara.org community",
}

from .config import app_dir

_PROJECT_ROOT = app_dir()
_CRASH_CODES = {3221225477, -1073741819, -11, 139}


def _describe_exit(code: int | None) -> str:
    if code is None:
        return "still running"
    if code in _CRASH_CODES:
        return f"crashed (exit code {code}, access violation)"
    return f"exited with code {code}"


class Transcriber:
    def __init__(self, model_size: str = "base.en", device: str = "auto",
                 compute_type: str = "int8", language: str | None = "en",
                 cpu_threads: int = 0):
        self.model_size = model_size
        self.device = "cpu" if device == "auto" else device
        self.compute_type = compute_type
        self.language = language or ""
        self.cpu_threads = int(cpu_threads or 0)

        self._proc: subprocess.Popen | None = None
        self._error: str | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._starting = False

    # -- the worker process ---------------------------------------------------

    def _spawn(self) -> subprocess.Popen:
        args = [self.model_size, self.device, self.compute_type,
                str(self.cpu_threads), self.language]
        if getattr(sys, "frozen", False):
            # Packaged: Whisper is its own program (see build_exe.py), because
            # it cannot share a process with the Windows OCR components.
            exe = "GameLensWorker.exe" if sys.platform == "win32" else "GameLensWorker"
            worker = _PROJECT_ROOT / "worker" / exe
            if worker.exists():
                cmd = [str(worker), *args]
            else:  # an older single-exe build: run ourselves in worker mode
                cmd = [sys.executable, "--stt-worker", *args]
        else:
            cmd = [sys.executable, "-X", "faulthandler", "-m",
                   "gamelens.stt_worker", *args]
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 if sys.platform == "win32" else 0)
        proc = subprocess.Popen(
            cmd, cwd=str(_PROJECT_ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=flags,
        )
        # Forward the worker's stderr (download progress, crash reports) to ours,
        # so it ends up in the console and the debug log.
        threading.Thread(target=self._drain_stderr, args=(proc,),
                         name="gamelens-stt-stderr", daemon=True).start()
        return proc

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line and "unauthenticated requests" not in line:
                    print(f"  [whisper] {line}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def _start_worker(self) -> None:
        """Launch the worker and wait for READY. Sets _error on failure."""
        self._error = None
        try:
            proc = self._spawn()
        except Exception as exc:
            self._error = f"could not start the speech worker: {exc}"
            return
        self._proc = proc

        while True:
            line = proc.stdout.readline()
            if not line:
                code = proc.wait()
                self._error = f"speech worker {_describe_exit(code)} while loading"
                self._proc = None
                return
            if line.strip() == b"READY":
                return

    # -- loading --------------------------------------------------------------

    def preload(self) -> None:
        """Start loading in the background; safe to call more than once."""
        with self._lock:
            if self._ready.is_set() or self._starting:
                return
            self._starting = True
        threading.Thread(target=self._load, name="gamelens-stt-load",
                         daemon=True).start()

    def _load(self) -> None:
        try:
            self._start_worker()
        finally:
            self._ready.set()

    @property
    def status(self) -> str:
        if self._error:
            return f"unavailable ({self._error})"
        if not self._ready.is_set():
            return "loading"
        return f"ready ({self.model_size}, separate process)"

    def wait_ready(self, timeout: float = 90.0) -> bool:
        self.preload()
        self._ready.wait(timeout)
        return self._proc is not None and self._error is None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # the worker exits when its input closes
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # -- transcription --------------------------------------------------------

    def _request(self, samples: np.ndarray) -> str:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise RuntimeError("worker not running")
        data = samples.astype(np.float32).tobytes()
        proc.stdin.write(f"{len(samples)}\n".encode("ascii") + data)
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                code = proc.wait()
                raise RuntimeError(f"speech worker {_describe_exit(code)}")
            if line.startswith(b"TEXT:"):
                return line[5:].decode("utf-8", errors="replace").strip()

    def transcribe(self, samples: np.ndarray, rate: int = MIC_RATE) -> str:
        if samples is None or len(samples) < rate * 0.25:
            return ""  # less than a quarter second: a mis-press
        if float(np.abs(samples).max(initial=0.0)) < 0.01:
            return ""  # silence; the mic was muted or the wrong device
        if not self.wait_ready():
            raise RuntimeError(f"speech recognition {self.status}")

        with self._lock:
            try:
                text = self._request(samples)
            except (RuntimeError, OSError) as first:
                # One automatic restart, then give up with a clear reason.
                print(f"  [whisper] {first} - restarting the speech worker",
                      file=sys.stderr, flush=True)
                self._start_worker()
                if self._error:
                    raise RuntimeError(self._error) from first
                text = self._request(samples)

        if text.lower().strip() in _HALLUCINATIONS:
            return ""
        return text
