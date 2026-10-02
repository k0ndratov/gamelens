"""Screen capture, subtitle isolation and change detection.

The expensive part of a tool like this is OCR, so the goal here is to run it as
rarely as possible: grab a small rectangle, threshold it to isolate light
subtitle text from whatever is happening behind it, and hash the result. The
hash is stable while the same line is on screen even though the background
moves, so a long line of dialogue costs exactly one OCR call no matter how many
frames it spans.
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np
from PIL import Image

from .native import LOCK as NATIVE_LOCK

# Rec. 601 luma weights, applied to mss's BGRA byte order.
_LUMA_BGR = np.array([0.114, 0.587, 0.299], dtype=np.float32)


@dataclass(frozen=True)
class Box:
    """An absolute pixel rectangle on the virtual desktop."""

    left: int
    top: int
    width: int
    height: int

    def as_mss(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


def list_monitors() -> list[dict]:
    import mss

    with mss.mss() as sct:
        # Index 0 is the union of all monitors; keep it, it is sometimes useful.
        return [dict(m) for m in sct.monitors]


def monitor_box(index: int) -> Box:
    monitors = list_monitors()
    if index < 0 or index >= len(monitors):
        raise IndexError(
            f"Monitor {index} does not exist; found {len(monitors) - 1} "
            f"monitor(s). Run with --list-monitors."
        )
    m = monitors[index]
    return Box(m["left"], m["top"], m["width"], m["height"])


def resolve_region(monitor: Box, region: dict) -> Box:
    """Turn fractional region settings into an absolute pixel box."""
    left = monitor.left + int(round(float(region["left"]) * monitor.width))
    top = monitor.top + int(round(float(region["top"]) * monitor.height))
    width = max(1, int(round(float(region["width"]) * monitor.width)))
    height = max(1, int(round(float(region["height"]) * monitor.height)))

    # Clamp to the monitor so a bad config can never crash the grab.
    left = min(max(left, monitor.left), monitor.left + monitor.width - 1)
    top = min(max(top, monitor.top), monitor.top + monitor.height - 1)
    width = min(width, monitor.left + monitor.width - left)
    height = min(height, monitor.top + monitor.height - top)
    return Box(left, top, width, height)


def to_gray(frame: np.ndarray) -> np.ndarray:
    """BGRA uint8 array from mss -> float32 luminance 0..255."""
    return frame[:, :, :3].astype(np.float32) @ _LUMA_BGR


def text_mask(gray: np.ndarray, threshold: int) -> np.ndarray:
    return gray >= float(threshold)


def mask_signature(mask: np.ndarray) -> str:
    """A hash that changes when the subtitle text changes, and not otherwise."""
    return hashlib.blake2b(np.packbits(mask).tobytes(), digest_size=16).hexdigest()


def prepare_for_ocr(gray: np.ndarray, mask: np.ndarray, scale: int = 2) -> Image.Image:
    """Clean dark-on-white image of just the subtitle text.

    Background pixels are flattened to white and text pixels are inverted, which
    keeps the anti-aliased edges OCR engines rely on while throwing away the
    game scene entirely.
    """
    inverted = np.where(mask, 255.0 - gray, 255.0)
    img = Image.fromarray(np.clip(inverted, 0, 255).astype(np.uint8), mode="L")
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    return img


def encode_jpeg(frame: np.ndarray, target_width: int, quality: int = 70) -> bytes:
    """BGRA array -> downscaled JPEG bytes, for sending to the model."""
    img = Image.fromarray(frame[:, :, :3][:, :, ::-1])  # BGR -> RGB
    if img.width > target_width:
        ratio = target_width / img.width
        img = img.resize((target_width, max(1, int(img.height * ratio))), Image.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


class CaptureWorker(threading.Thread):
    """Samples the subtitle strip and calls back with text that actually changed.

    on_text(text, timestamp)  - a newly OCR'd subtitle line
    on_clear(timestamp)       - the subtitle area went empty
    on_scene(jpeg_bytes, ts)  - a fresh downscaled full-screen frame
    on_error(exception)       - something went wrong; the loop keeps running
    """

    def __init__(
        self,
        config,
        ocr_engine,
        on_text: Callable[[str, float], None],
        on_clear: Callable[[float], None] | None = None,
        on_scene: Callable[[bytes, float], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_crop: Callable[[Image.Image, str, float], None] | None = None,
    ):
        super().__init__(name="gamelens-capture", daemon=True)
        self.on_crop = on_crop
        self.cfg = config
        self.ocr = ocr_engine
        self.on_text = on_text
        self.on_clear = on_clear
        self.on_scene = on_scene
        self.on_error = on_error
        self._halt = threading.Event()

        cap = config.capture
        self.interval = 1.0 / max(0.2, float(cap["fps"]))
        self.threshold = int(cap["text_threshold"])
        self.min_fraction = float(cap["min_text_fraction"])
        self.scene_interval = float(cap["scene_frame_interval"])
        self.scene_width = int(cap["scene_frame_width"])
        self.monitor_index = int(cap["monitor"])

        # Diagnostics, surfaced by the panel's status line.
        self.frames_sampled = 0
        self.ocr_calls = 0
        self.last_error: str | None = None

    def stop(self) -> None:
        self._halt.set()

    @property
    def region(self) -> Box:
        return resolve_region(monitor_box(self.monitor_index), self.cfg.capture["region"])

    def run(self) -> None:  # pragma: no cover - needs a real screen
        import mss

        last_sig: str | None = None
        was_empty = True
        next_scene = 0.0

        # mss handles are not thread-safe, so this one belongs to this thread.
        with mss.mss() as sct:
            mon = monitor_box(self.monitor_index)
            crop = resolve_region(mon, self.cfg.capture["region"]).as_mss()
            full = mon.as_mss()

            while not self._halt.is_set():
                started = time.monotonic()
                try:
                    frame = np.asarray(sct.grab(crop))
                    self.frames_sampled += 1
                    gray = to_gray(frame)
                    mask = text_mask(gray, self.threshold)
                    fraction = float(mask.mean())

                    if fraction < self.min_fraction:
                        if not was_empty:
                            was_empty = True
                            last_sig = None
                            if self.on_clear:
                                self.on_clear(time.time())
                    else:
                        was_empty = False
                        sig = mask_signature(mask)
                        if sig != last_sig:
                            last_sig = sig
                            with NATIVE_LOCK:
                                text = self.ocr.read(prepare_for_ocr(gray, mask))
                            self.ocr_calls += 1
                            if text:
                                now = time.time()
                                self.on_text(text, now)
                                if self.on_crop:
                                    crop_rgb = Image.fromarray(
                                        np.ascontiguousarray(frame[:, :, 2::-1]))
                                    self.on_crop(crop_rgb, text, now)

                    if self.on_scene and started >= next_scene:
                        next_scene = started + self.scene_interval
                        scene = np.asarray(sct.grab(full))
                        self.on_scene(encode_jpeg(scene, self.scene_width), time.time())

                except Exception as exc:  # keep the loop alive; report once
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if self.on_error:
                        self.on_error(exc)
                    time.sleep(0.5)

                elapsed = time.monotonic() - started
                self._halt.wait(max(0.0, self.interval - elapsed))


def sample_once(config, ocr_engine) -> tuple[str, float, Image.Image]:
    """Grab the subtitle region once. Used by the doctor/calibration commands."""
    import mss

    mon = monitor_box(int(config.capture["monitor"]))
    crop = resolve_region(mon, config.capture["region"])
    with mss.mss() as sct:
        frame = np.asarray(sct.grab(crop.as_mss()))
    gray = to_gray(frame)
    mask = text_mask(gray, int(config.capture["text_threshold"]))
    prepared = prepare_for_ocr(gray, mask)
    text = ocr_engine.read(prepared) if ocr_engine else ""
    return text, float(mask.mean()), prepared


def grab_monitor(index: int) -> Image.Image:
    """Full-resolution screenshot of one monitor, as a PIL image."""
    import mss

    mon = monitor_box(index)
    with mss.mss() as sct:
        frame = np.asarray(sct.grab(mon.as_mss()))
    return Image.fromarray(frame[:, :, :3][:, :, ::-1])


def iter_region_previews(config) -> Iterable[Image.Image]:  # pragma: no cover
    """Yield prepared subtitle crops forever - handy when tuning the threshold."""
    import mss

    mon = monitor_box(int(config.capture["monitor"]))
    crop = resolve_region(mon, config.capture["region"]).as_mss()
    with mss.mss() as sct:
        while True:
            frame = np.asarray(sct.grab(crop))
            gray = to_gray(frame)
            yield prepare_for_ocr(gray, text_mask(gray, int(config.capture["text_threshold"])))
            time.sleep(0.5)
