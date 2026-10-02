"""Configuration loading and saving."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "capture": {
        "monitor": 1,
        "region": {"left": 0.15, "top": 0.78, "width": 0.70, "height": 0.16},
        "fps": 3.0,
        "text_threshold": 170,
        "min_text_fraction": 0.002,
        "keep_scene_frames": 6,
        "scene_frame_interval": 1.5,
        "scene_frame_width": 1280,
    },
    "buffer": {"minutes": 5, "dedupe_ratio": 0.88, "min_line_chars": 3},
    "ocr": {"backends": ["windows", "rapidocr", "tesseract"], "language": "en"},
    "hotkeys": {
        "method": "poll",
        "talk": "<ctrl>+<alt>+q",
        "mode": "hold",
        "repeat": "<ctrl>+<alt>+w",
        "stop": "<ctrl>+<alt>+s",
    },
    "mic": {"input_device": None},
    "stt": {
        "model": "base.en",
        "device": "auto",
        "compute_type": "int8",
        "cpu_threads": 0,
        "language": "en",
    },
    "voice": {
        "voice": "en-US-AriaNeural",
        "rate": "+10%",
        "volume": "+0%",
        "pitch": "+0Hz",
        "output_device": None,
        "duck_game_volume": 0.35,
        "beeps": True,
    },
    "model": {
        "name": "claude-sonnet-4-5",
        "max_tokens": 500,
        "attach_screenshot": True,
        # Used only for the cost estimate shown in the window (USD per million
        # tokens). Check anthropic.com/pricing and adjust if they change.
        "price_input_per_mtok": 3.0,
        "price_output_per_mtok": 15.0,
    },
    "screenshots": {
        "enabled": True,
        "folder": "screenshots",
        "keep_subtitle_crops": 100,
        "keep_game_frames": 10,
    },
    "help": {
        "style": "plain",
        "level": "B2",
        "default_question": "What did they just say? Put it in simpler English.",
        "game": None,
    },
    "log": {"file": "gamelens-session.log"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def app_dir() -> Path:
    """Where config.yaml, .env, logs and screenshots live.

    The project root when run from source; the folder containing the .exe when
    packaged, so a packaged app keeps its settings beside itself.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    return app_dir() / "config.yaml"


class Config:
    def __init__(self, data: dict, path: Path):
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else default_config_path()
        raw: dict = {}
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        return cls(_deep_merge(DEFAULTS, raw), p)

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self.data, fh, sort_keys=False, allow_unicode=True)

    def get(self, dotted: str, fallback: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return fallback
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    # Convenience accessors used across the app -------------------------------

    @property
    def capture(self) -> dict:
        return self.data["capture"]

    @property
    def buffer(self) -> dict:
        return self.data["buffer"]

    @property
    def ocr(self) -> dict:
        return self.data["ocr"]

    @property
    def model(self) -> dict:
        return self.data["model"]

    @property
    def screenshots(self) -> dict:
        return self.data["screenshots"]

    @property
    def hotkeys(self) -> dict:
        return self.data["hotkeys"]

    @property
    def voice(self) -> dict:
        return self.data["voice"]

    @property
    def stt(self) -> dict:
        return self.data["stt"]

    @property
    def mic(self) -> dict:
        return self.data["mic"]

    @property
    def help(self) -> dict:
        return self.data["help"]
