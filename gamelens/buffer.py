"""The rolling dialogue buffer.

Holds the last few minutes of subtitle text and nothing else. OCR on a
changing frame often returns the same line twice with one character different,
so lines are merged by similarity rather than by exact match, and the longest
variant wins - that is almost always the fully-rendered line.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher

_NORM = re.compile(r"[^a-z0-9а-яё ]+")
_SPACES = re.compile(r"\s+")

# Below this length, containment is too weak a signal to merge on: a line of
# "Yes." really can be followed by "Yes. And I told him so."
_MIN_CONTAINMENT_CHARS = 8


def normalise(text: str) -> str:
    """Lowercase, punctuation-free form used for containment checks."""
    return _SPACES.sub(" ", _NORM.sub("", (text or "").lower())).strip()


@dataclass
class Line:
    text: str
    first_seen: float
    last_seen: float

    @property
    def age(self) -> float:
        return time.time() - self.last_seen


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class DialogueBuffer:
    """Thread-safe, time-bounded list of subtitle lines."""

    def __init__(self, minutes: float = 5.0, dedupe_ratio: float = 0.88,
                 min_line_chars: int = 3):
        self.window = float(minutes) * 60.0
        self.dedupe_ratio = float(dedupe_ratio)
        self.min_line_chars = int(min_line_chars)
        self._lines: deque[Line] = deque()
        self._lock = threading.RLock()
        self._scenes: deque[tuple[bytes, float]] = deque(maxlen=6)

    # -- writing --------------------------------------------------------------

    def add(self, text: str, timestamp: float | None = None) -> bool:
        """Record a line. Returns True if it was genuinely new."""
        text = (text or "").strip()
        if len(text) < self.min_line_chars:
            return False
        ts = timestamp or time.time()

        with self._lock:
            self._prune(ts)
            if self._lines:
                last = self._lines[-1]
                ratio = similarity(last.text, text)
                # Punctuation-insensitive containment catches a subtitle that is
                # still being revealed, where the trailing comma or period moves.
                new_norm, last_norm = normalise(text), normalise(last.text)
                shorter = min(len(new_norm), len(last_norm))
                long_enough = shorter >= _MIN_CONTAINMENT_CHARS
                extends = (
                    long_enough
                    and len(new_norm) > len(last_norm)
                    and last_norm in new_norm
                )
                contained = long_enough and (
                    new_norm in last_norm or last_norm in new_norm
                )

                if extends:
                    # The line is still being revealed - take the fuller version.
                    last.text = text
                    last.last_seen = ts
                    return False
                if ratio >= self.dedupe_ratio or contained:
                    # A re-read of the same line. Keep the text already stored
                    # rather than letting OCR wobble ("coming" -> "corning")
                    # overwrite a good read with a longer bad one.
                    last.last_seen = ts
                    return False
            self._lines.append(Line(text=text, first_seen=ts, last_seen=ts))
            return True

    def mark_gap(self, timestamp: float | None = None) -> None:
        """The subtitle area went empty - the next line starts fresh."""
        with self._lock:
            self._prune(timestamp or time.time())

    def add_scene(self, jpeg: bytes, timestamp: float | None = None) -> None:
        with self._lock:
            self._scenes.append((jpeg, timestamp or time.time()))

    def set_scene_capacity(self, n: int) -> None:
        with self._lock:
            self._scenes = deque(self._scenes, maxlen=max(1, int(n)))

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._scenes.clear()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._lines and self._lines[0].last_seen < cutoff:
            self._lines.popleft()

    # -- reading --------------------------------------------------------------

    def lines(self, limit: int | None = None) -> list[Line]:
        with self._lock:
            self._prune(time.time())
            items = list(self._lines)
        return items[-limit:] if limit else items

    def latest_scene(self) -> bytes | None:
        with self._lock:
            return self._scenes[-1][0] if self._scenes else None

    def transcript(self, limit: int | None = None, relative: bool = True) -> str:
        """The buffer rendered for the prompt, newest last.

        Timestamps are relative ("-0:12") because what matters to the model is
        how long ago a line was said, not the wall clock.
        """
        items = self.lines(limit)
        if not items:
            return ""
        now = time.time()
        out = []
        for line in items:
            if relative:
                delta = max(0, int(round(now - line.last_seen)))
                stamp = f"-{delta // 60}:{delta % 60:02d}"
            else:
                stamp = time.strftime("%H:%M:%S", time.localtime(line.last_seen))
            out.append(f"[{stamp}] {line.text}")
        return "\n".join(out)

    def last_text(self) -> str:
        items = self.lines(1)
        return items[0].text if items else ""

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)
