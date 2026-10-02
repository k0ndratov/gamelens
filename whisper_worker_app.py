"""Entry point for the packaged Whisper worker (worker/GameLensWorker.exe).

Kept separate from GameLens.exe on purpose: the speech engine and the Windows
OCR components crash when loaded into the same process, so they are packaged
as two programs and talk over a pipe.
"""

from gamelens.stt_worker import main

if __name__ == "__main__":
    raise SystemExit(main())
