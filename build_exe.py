"""Builds dist/GameLens/GameLens.exe (plus its Whisper worker) with PyInstaller.

Run through build.bat. Two programs are built, because the speech engine
(CTranslate2 / ONNX Runtime) and the Windows OCR components crash when they are
loaded into one process - and PyInstaller itself loads every bundled package
into one process while scanning for DLLs. So:

    dist/GameLens/GameLens.exe               the window, OCR, hotkeys, voice
    dist/GameLens/worker/GameLensWorker.exe  Whisper only, started by the window

Everything printed is also written to build.log next to this file.
Settings already in dist/GameLens (config.yaml, .env, usage.json) survive a
rebuild.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "GameLens"
WORKER_DIST = ROOT / "build" / "worker-dist"
KEEP = ("config.yaml", ".env", "usage.json")

SPEECH = ["faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers",
          "huggingface_hub"]
WINDOW_SIDE = ["winocr", "winrt", "winsdk", "anthropic", "edge_tts", "comtypes",
               "pycaw", "pynput", "mss", "tkinter", "_tkinter", "sounddevice",
               "_sounddevice_data", "miniaudio", "aiohttp"]
NEVER = ["onnxruntime.quantization", "onnxruntime.transformers",
         "onnxruntime.tools", "onnxruntime.datasets", "comtypes.test",
         "certifi.tests", "numpy.f2py", "huggingface_hub.cli",
         "huggingface_hub.inference", "av.datasets", "torch", "tensorflow",
         "matplotlib", "IPython", "pytest"]

BUILDS = {
    "worker": {
        "entry": "whisper_worker_app.py",
        "name": "GameLensWorker",
        "distpath": WORKER_DIST,
        "collect_all": ["faster_whisper"],
        "collect_binaries": ["ctranslate2", "onnxruntime", "av", "tokenizers"],
        "hidden": ["gamelens.stdio"],
        "exclude": WINDOW_SIDE + NEVER,
        "admin": False,
    },
    "window": {
        "entry": "gamelens_app.py",
        "name": "GameLens",
        "distpath": ROOT / "dist",
        "collect_all": ["_sounddevice_data", "winocr", "winrt", "winsdk",
                        "edge_tts"],
        "collect_binaries": [],
        "hidden": ["pynput.keyboard._win32", "pynput.mouse._win32",
                   "mss.windows", "gamelens.__main__", "gamelens.calibrate",
                   "gamelens.gui",
                   # miniaudio and sounddevice load this C extension at runtime;
                   # PyInstaller does not see it, and without it no answer plays.
                   "_cffi_backend", "miniaudio", "sounddevice"],
        "exclude": SPEECH + NEVER,
        "admin": True,     # the game runs elevated, so the window must too
    },
}


def present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def pyinstaller_args(spec: dict) -> list[str]:
    args = [
        str(ROOT / spec["entry"]),
        "--name", spec["name"],
        "--noconfirm", "--clean",
        "--windowed",
        "--distpath", str(spec["distpath"]),
        "--workpath", str(ROOT / "build" / spec["name"]),
        "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT),
    ]
    if spec["admin"]:
        args.append("--uac-admin")
    icon = ROOT / "gamelens.ico"
    if icon.exists():
        args += ["--icon", str(icon)]
    for package in spec["collect_all"]:
        if present(package):
            args += ["--collect-all", package]
    for package in spec["collect_binaries"]:
        if present(package):
            args += ["--collect-binaries", package, "--collect-data", package]
    for module in spec["hidden"]:
        args += ["--hidden-import", module]
    for module in spec["exclude"]:
        args += ["--exclude-module", module]
    return args


def run_logged(cmd: list[str], log) -> int:
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    for line in proc.stdout:
        sys.stdout.write(line)
        log.write(line)
    return proc.wait()


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    log_path = ROOT / "build.log"
    with log_path.open("w", encoding="utf-8") as log:
        def say(text: str) -> None:
            print(text, flush=True)
            log.write(text + "\n")
            log.flush()

        if not dry_run and not present("PyInstaller"):
            say("PyInstaller is missing - build.bat installs it; run that instead.")
            return 1

        # Keep settings that were changed inside the packaged app.
        saved = {}
        for name in KEEP:
            if (DIST / name).exists():
                saved[name] = (DIST / name).read_bytes()

        for label in ("worker", "window"):
            args = pyinstaller_args(BUILDS[label])
            say(f"\n===== building the {label} =====")
            if dry_run:
                say(" ".join(args))
                continue
            code = run_logged([sys.executable, "-m", "PyInstaller", *args], log)
            if code != 0:
                say(f"\nBUILD FAILED while building the {label} (exit code {code}).")
                say(f"Full output: {log_path}")
                return 1

        if dry_run:
            return 0

        # Put the worker inside the window's folder.
        target = DIST / "worker"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(WORKER_DIST / "GameLensWorker", target)

        # Settings: keep what was in the packaged app, otherwise copy the project's.
        for name in ("config.yaml", ".env", "usage.json", "gamelens.ico"):
            if name in saved:
                (DIST / name).write_bytes(saved[name])
            elif (ROOT / name).exists():
                shutil.copy2(ROOT / name, DIST / name)

        say(f"\nBuilt: {DIST / 'GameLens.exe'}")
        say(f"       {target / 'GameLensWorker.exe'}")
        make_shortcut(DIST / "GameLens.exe", say)
    return 0


def make_shortcut(target: Path, say) -> None:
    """Desktop shortcut, via the Windows Script Host (no extra packages)."""
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        "[Environment]::GetFolderPath('Desktop') + '\\GameLens.lnk');"
        f"$s.TargetPath = '{target}';"
        f"$s.WorkingDirectory = '{target.parent}';"
        f"$s.IconLocation = '{target}';"
        "$s.Save()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       check=True, capture_output=True)
        say("Desktop shortcut created: GameLens")
    except Exception as exc:
        say(f"(Could not create a desktop shortcut: {exc})")


if __name__ == "__main__":
    sys.exit(main())
