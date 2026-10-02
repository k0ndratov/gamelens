"""Make sure stdin/stdout/stderr exist.

A windowless program (pythonw.exe, or the packaged GameLens.exe) starts with
sys.stdout and friends set to None. When such a process is launched with pipes
for its standard handles - as the Whisper worker and the calibration helper
are - the pipes are there at the OS level, Python just has not wrapped them.
This wraps them, so print() and the worker protocol work in every mode.
"""

from __future__ import annotations

import os
import sys


def ensure_std_streams() -> None:
    specs = ((0, "stdin", "r"), (1, "stdout", "w"), (2, "stderr", "w"))
    for fd, name, mode in specs:
        if getattr(sys, name) is not None:
            continue
        try:
            stream = os.fdopen(fd, mode, buffering=1, encoding="utf-8",
                               errors="replace", closefd=False)
        except (OSError, ValueError):
            continue
        setattr(sys, name, stream)
