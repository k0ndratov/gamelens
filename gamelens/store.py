"""Keeps the most recent screenshots on disk so they can be looked at.

Two folders under one root:
    subtitles/   the cropped subtitle strip each time a new line was read
    frames/      the full game frame, roughly every second and a half
Each is pruned to a fixed count, so the folder never grows without bound.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(text: str, limit: int = 40) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:limit] or "line"


class ScreenshotStore:
    def __init__(self, root: Path, keep_crops: int = 100, keep_frames: int = 10,
                 enabled: bool = True):
        self.root = Path(root)
        self.crops_dir = self.root / "subtitles"
        self.frames_dir = self.root / "frames"
        self.keep_crops = max(1, int(keep_crops))
        self.keep_frames = max(1, int(keep_frames))
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self.crops_saved = 0
        self.frames_saved = 0
        self.last_error: str | None = None
        if self.enabled:
            self.ensure()

    def ensure(self) -> None:
        for folder in (self.crops_dir, self.frames_dir):
            folder.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _stamp(ts: float) -> str:
        whole = int(ts)
        return time.strftime("%H-%M-%S", time.localtime(whole)) + f".{int((ts - whole) * 1000):03d}"

    def _prune(self, folder: Path, keep: int) -> None:
        try:
            files = sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for old in files[:-keep] if len(files) > keep else []:
            try:
                old.unlink()
            except OSError:
                pass

    def save_crop(self, image, text: str, ts: float | None = None) -> Path | None:
        """image is a PIL image of the subtitle strip."""
        if not self.enabled:
            return None
        ts = ts or time.time()
        with self._lock:
            try:
                self.ensure()
                path = self.crops_dir / f"{self._stamp(ts)}_{_slug(text)}.png"
                image.save(path, format="PNG")
                self.crops_saved += 1
                self._prune(self.crops_dir, self.keep_crops)
                return path
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                return None

    def save_frame(self, jpeg: bytes, ts: float | None = None) -> Path | None:
        if not self.enabled or not jpeg:
            return None
        ts = ts or time.time()
        with self._lock:
            try:
                self.ensure()
                path = self.frames_dir / f"{self._stamp(ts)}.jpg"
                path.write_bytes(jpeg)
                self.frames_saved += 1
                self._prune(self.frames_dir, self.keep_frames)
                return path
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                return None

    def counts(self) -> tuple[int, int]:
        """(subtitle crops, game frames) currently on disk."""
        def count(folder: Path) -> int:
            try:
                return sum(1 for _ in folder.iterdir())
            except OSError:
                return 0
        return count(self.crops_dir), count(self.frames_dir)

    def open_folder(self) -> None:
        self.ensure()
        if sys.platform == "win32":
            os.startfile(str(self.root))  # noqa: S606 - opening a folder we own
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(self.root)])
        else:
            subprocess.Popen(["xdg-open", str(self.root)])
