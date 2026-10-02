"""OCR backends, tried in preference order.

Three are supported, and they trade off differently:

windows    Windows.Media.Ocr, built into Windows 10/11. No model download, runs
           in a few milliseconds, accurate on clean screen text. Best choice.
rapidocr   ONNX models bundled with the pip package. Cross-platform, slower
           (~100ms on CPU) but needs nothing installed outside Python.
tesseract  Needs tesseract.exe installed separately. Last resort; it is the
           least accurate of the three on game subtitles.
"""

from __future__ import annotations

import asyncio
import re
from typing import Protocol

from PIL import Image

_WS = re.compile(r"\s+")
# OCR on stylised game fonts likes to invent these.
_FIXUPS = [
    (re.compile(r"\|"), "I"),
    (re.compile(r"(?<=[a-z])0(?=[a-z])"), "o"),
    (re.compile(r"\s+([,.!?;:])"), r"\1"),
    (re.compile(r"''"), '"'),
]


def clean_text(raw: str) -> str:
    """Normalise an OCR result into a single tidy line."""
    if not raw:
        return ""
    text = raw.replace("\r", " ").replace("\n", " ")
    text = _WS.sub(" ", text).strip()
    for pattern, replacement in _FIXUPS:
        text = pattern.sub(replacement, text)
    # Strip stray single characters left over from UI chrome at the edges.
    if len(text) <= 2 and not text.isalpha():
        return ""
    return text.strip(" -_|")


class OcrBackend(Protocol):
    name: str

    def read(self, image: Image.Image) -> str: ...


class WindowsOcr:
    """Windows.Media.Ocr via the `winocr` wrapper."""

    name = "windows"

    def __init__(self, language: str = "en"):
        import winocr  # noqa: F401  (import is the availability check)

        self._winocr = winocr
        self.language = language
        self._sync = getattr(winocr, "recognize_pil_sync", None)
        if self._sync is None and not hasattr(winocr, "recognize_pil"):
            raise RuntimeError("winocr is installed but exposes no recognise function")
        # Fail loudly here rather than on the first subtitle.
        self.read(Image.new("L", (32, 32), color=255))

    def _result_text(self, result) -> str:
        if result is None:
            return ""
        if isinstance(result, dict):
            return result.get("text", "") or ""
        return getattr(result, "text", "") or ""

    def read(self, image: Image.Image) -> str:
        rgb = image.convert("RGB")
        if self._sync is not None:
            result = self._sync(rgb, self.language)
        else:
            result = asyncio.run(self._winocr.recognize_pil(rgb, self.language))
        return clean_text(self._result_text(result))


class RapidOcr:
    """rapidocr-onnxruntime: bundled ONNX models, no external binaries."""

    name = "rapidocr"

    def __init__(self, language: str = "en"):
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR

        self._np = np
        self._engine = RapidOCR()

    def read(self, image: Image.Image) -> str:
        arr = self._np.array(image.convert("RGB"))
        result, _elapsed = self._engine(arr)
        if not result:
            return ""
        # Each entry is [box, text, confidence]; keep reading order.
        parts = [str(entry[1]) for entry in result if len(entry) > 1 and entry[1]]
        return clean_text(" ".join(parts))


class TesseractOcr:
    name = "tesseract"

    def __init__(self, language: str = "en"):
        import pytesseract

        self._pt = pytesseract
        self.language = {"en": "eng", "ru": "rus"}.get(language, language)
        # Raises if the tesseract binary is missing.
        self._pt.get_tesseract_version()

    def read(self, image: Image.Image) -> str:
        raw = self._pt.image_to_string(
            image, lang=self.language, config="--psm 6 --oem 1"
        )
        return clean_text(raw)


class NullOcr:
    """Placeholder so the app can still start and explain what is missing."""

    name = "none"

    def read(self, image: Image.Image) -> str:
        return ""


_BACKENDS = {
    "windows": WindowsOcr,
    "rapidocr": RapidOcr,
    "tesseract": TesseractOcr,
}


def build_engine(
    preference: list[str], language: str = "en"
) -> tuple[OcrBackend, list[str]]:
    """Return the first working backend, plus notes on the ones that failed."""
    notes: list[str] = []
    for key in preference:
        factory = _BACKENDS.get(key)
        if factory is None:
            notes.append(f"{key}: unknown backend name")
            continue
        try:
            engine = factory(language)
            notes.append(f"{key}: ready")
            return engine, notes
        except ImportError as exc:
            notes.append(f"{key}: not installed ({exc.name or exc})")
        except Exception as exc:
            notes.append(f"{key}: unavailable ({type(exc).__name__}: {exc})")
    notes.append("no OCR backend available - install winocr or rapidocr-onnxruntime")
    return NullOcr(), notes
