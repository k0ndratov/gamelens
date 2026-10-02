"""Whisper in its own process.

CTranslate2 (the engine under faster-whisper) crashes with an access violation
when it is loaded into a process that already holds certain other native
libraries - here, the Windows OCR components. Run on its own it is fine. So the
model lives in this child process, which imports nothing but numpy and
faster-whisper, and the main program talks to it over a pipe.

Protocol, on stdin/stdout:
    child -> parent   "READY\\n" once the model is loaded
    parent -> child   "<n>\\n" then n float32 samples (16 kHz mono) as raw bytes
    child -> parent   "TEXT:<transcript>\\n"
"""

from __future__ import annotations

import faulthandler
import os
import sys


def _read_exact(stream, count: int) -> bytes:
    chunks, remaining = [], count
    while remaining > 0:
        block = stream.read(remaining)
        if not block:
            raise EOFError("parent closed the pipe")
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def main() -> int:
    from gamelens.stdio import ensure_std_streams

    ensure_std_streams()
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except (AttributeError, ValueError, OSError):
        pass
    model_name, device, compute, threads, language = sys.argv[1:6]

    # The protocol uses the raw pipe handles directly. Going through sys.stdout
    # would break in a windowless exe: once sys.stdout is pointed elsewhere, its
    # old wrapper is garbage-collected and closes the buffer underneath us.
    proto = os.fdopen(1, "wb", buffering=0, closefd=False)
    inp = os.fdopen(0, "rb", closefd=False)
    # Anything a library prints goes to stderr, never into the protocol.
    sys.stdout = sys.stderr

    import numpy as np

    fake = model_name.startswith("__fake")
    model = None
    if model_name == "__fake_crash__":
        os._exit(3)  # dies before READY, like a native crash on load
    if model_name == "__selfcheck__":
        # Packaging check without a model download: the native engine loads,
        # and the bundled voice-activity model (ONNX) runs on real audio.
        import ctranslate2
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        noise = (np.random.default_rng(0).normal(0, 0.05, 16000 * 2)
                 .astype(np.float32))
        segments = get_speech_timestamps(noise, VadOptions())
        proto.write(b"READY\n")
        proto.write((f"TEXT:selfcheck ok - ctranslate2 {ctranslate2.__version__}, "
                     f"vad ran ({len(segments)} speech segments in noise)\n")
                    .encode("utf-8"))
        proto.flush()
        return 0
    if not fake:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name, device=device, compute_type=compute,
                             cpu_threads=int(threads))

    proto.write(b"READY\n")
    proto.flush()

    while True:
        header = inp.readline()
        if not header:
            return 0  # parent went away
        count = int(header.strip())
        samples = np.frombuffer(_read_exact(inp, count * 4), dtype=np.float32)

        if model_name == "__fake_die__":
            os._exit(4)  # dies mid-request
        if fake:
            text = f"fake heard {count} samples"
        else:
            segments, _info = model.transcribe(
                samples,
                language=None if language in ("", "None") else language,
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()

        proto.write(("TEXT:" + text.replace("\r", " ").replace("\n", " ")
                     + "\n").encode("utf-8"))
        proto.flush()


if __name__ == "__main__":
    raise SystemExit(main())
