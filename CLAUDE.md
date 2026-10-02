# GameLens — handoff notes

Read this first. It explains what this project is, how it is used, how it
works, and — most importantly — why it is built the way it is. Most of the odd
design choices below exist because something crashed on this exact PC; please
don't undo them without reading why.

## What it is

A helper for practising English while playing games (built for The Witcher 3,
works with any game that shows subtitles). The owner is a B2-level English
learner. During a conversation in the game he presses a key, asks out loud
"what did he just say?", and hears a short, simple explanation in his headset
a couple of seconds later. Nothing is drawn over the game.

How it works, end to end:

1. It grabs only the subtitle strip of the screen, 3 times a second.
2. It keeps the bright text pixels and hashes them. If the hash didn't change,
   the same line is still on screen, so OCR is skipped. Most frames are skipped
   this way (the window shows the exact share).
3. Changed frames go through Windows' built-in OCR. The text goes into a rolling
   buffer holding the last 5 minutes of dialogue (text only, in memory).
4. Hold `Ctrl+Alt+Q` → the mic records → local Whisper turns speech into text.
5. The question, the 5-minute transcript and the current game frame go to
   Claude (Anthropic API, streamed).
6. Each finished sentence of the answer is spoken immediately (edge-tts, a
   British male voice) while the next one is still being written.

## How to use it

### Day to day

- **Window:** `start-gui.bat` (no build needed), or `GameLens.exe` once built.
  Press **Start**, then play.
- **Hold `Ctrl+Alt+Q`** — ask out loud, release, hear the answer.
- **`Ctrl+Alt+W`** — explain the last line, no speaking needed.
- **`Ctrl+Alt+S`** — stop the answer.
- The window shows subtitles as they are read, questions and answers, API cost
  (session and all-time), capture counters, and buttons for the screenshots
  folder (last 100 subtitle strips + last 10 game frames), calibration,
  settings and the debug log.

### Requirements on this PC

- The game must be in **Borderless Window** mode with subtitles on. In true
  fullscreen, screen capture gets a black frame.
- GameLens must run **as administrator**: the game runs elevated, and Windows
  hides an elevated window's keystrokes from normal programs.
- An Anthropic API key in `.env` with credit on it. API credit is billed at
  console.anthropic.com and is **separate** from the owner's Claude Max
  subscription — that confused things once already.

### Console commands (all `.venv\Scripts\python.exe -m gamelens <cmd>`)

| command | what it does |
| --- | --- |
| `run` | the whole thing, console mode |
| `calibrate` | 6-second countdown, then drag a box around the subtitle |
| `doctor` | checks screen, OCR, mic, voice, Whisper, API |
| `keytest` | shows what the hotkey reader sees |
| `whispertest` | finds Whisper settings that load on this CPU |
| `selftest` | checks every part loads — mainly for the packaged exe |
| `watch` | prints captured subtitles, no voice |
| `voices` | lists edge-tts voices |

Tests: `python tests\test_core.py` (logic, no hardware needed) and
`python tests\test_gui.py` (window; needs a display).

## The current task: build the .exe

Run `build.bat`. It installs PyInstaller, runs `build_exe.py`, and produces:

```
dist\GameLens\GameLens.exe               the window (asks for admin)
dist\GameLens\worker\GameLensWorker.exe  Whisper only, started by the window
```

plus a desktop shortcut, then runs `GameLens.exe --cli selftest` elevated.
Everything the build prints is also in `build.log`; the self-test result is in
`dist\GameLens\gamelens-debug.log`.

The build has **not yet succeeded on this PC**. The first attempt crashed
inside PyInstaller (see "PyInstaller crash" below); the second stopped at
the same stage, most likely for the same reason. The two-program build is
the fix; it was verified on Linux (both programs built, the window found the
worker, the Whisper engine and its VAD model ran inside the packaged worker),
but not yet on Windows. If it fails, read `build.log`, fix, rebuild.

Note: `GameLens.exe` has an admin manifest (`--uac-admin`), so launching it
from a non-elevated shell fails with error 740 — the owner has to approve the
UAC prompt himself.

## Why it is built this way — problems already solved

Each of these was found by real failures on this PC. Keep them.

**Hotkeys didn't work inside the game.** The game runs as administrator, so a
normal keyboard hook never sees its keystrokes. Fix: hotkeys are read by
polling `GetAsyncKeyState` (`hotkeys.PollHotkeys`, the default `method: poll`)
and GameLens runs elevated (`run-admin.bat`, `start-gui.bat`, the exe
manifest). Keys are matched by virtual-key code, not character, because with
Ctrl held Windows reports `Ctrl+Q` as `\x11`, and on the owner's keyboard
layout the Q key reads back as `ä`.

**Whisper crashed the whole process (access violation).** CTranslate2 (the
engine under faster-whisper) crashes when it is loaded into a process that
already has the Windows OCR (WinRT) components loaded. On its own it works fine
(`whispertest` proved every compute type loads). Fix: Whisper always runs in a
**separate worker process** (`stt_worker.py`), talking over a pipe:
`READY\n`, then `<n>\n` + n float32 samples → `TEXT:<transcript>\n`. Never
import faster_whisper / ctranslate2 into the main process.

**PyInstaller crash during the build.** While scanning for DLLs, PyInstaller
imports every bundled package into one process — which puts CTranslate2/ONNX
and WinRT together again, and it crashed (exit code 3221225477). Fix: build
**two programs** (`build_exe.py`): the window excludes all speech packages, the
worker excludes all window-side packages. Also: never `--collect-all` the big
speech libraries (it drags in `onnxruntime.quantization`, which crashed too);
they are listed under `--collect-binaries` instead.

**Silent answers in the packaged app.** `miniaudio` and `sounddevice` need the
C extension `_cffi_backend`, which PyInstaller doesn't detect. It is a hidden
import in `build_exe.py`. Found by building on Linux and running `selftest`.

**Windowless processes have no stdout.** A windowless exe (or `pythonw`) starts
with `sys.stdout = None`. `stdio.ensure_std_streams()` reattaches the pipe
handles, and the worker opens fds 0/1 directly with `os.fdopen(...,
closefd=False)`. Going through `sys.stdout.buffer` broke: once `sys.stdout`
was reassigned, the old wrapper got garbage-collected and closed the buffer.

**Calibration captured the wrong window.** The screenshot used to be taken
instantly, catching the terminal instead of the game. It now counts down 6
seconds so the owner can switch to the game first.

**Smaller things:** `doctor` once hid the voice test behind a slow Whisper
load (it now tests voice first). TTS errors used to be swallowed silently (they
are now printed). The configured model id gets auto-corrected to one the key
can use.

## Map of the code

```
gamelens/
  app.py         the engine; start()/stop(), events for the window, counters
  gui.py         tkinter window; also routes --cli / --stt-worker in the exe
  capture.py     screen grab, threshold + hash, change detection
  ocr.py         OCR backends: windows (winocr), rapidocr, tesseract
  buffer.py      rolling 5-minute dialogue buffer, fuzzy de-duplication
  ask.py         Claude prompt (spoken style), streaming, sentence splitting
  stt.py         launches and talks to the Whisper worker
  stt_worker.py  the Whisper worker process
  tts.py         edge-tts, sentence-pipelined, SAPI fallback, game ducking
  audio.py       mic recorder, player, volume ducking
  hotkeys.py     polling hotkeys (default) and the older hook method
  store.py       screenshots folder, pruned to fixed counts
  usage.py       token counts and estimated cost (usage.json)
  calibrate.py   drag-a-box subtitle calibration
  config.py      config.yaml with defaults; app_dir() handles the exe case
  native.py      lock so OCR calls never overlap
  stdio.py       reattach stdio in windowless processes
  __main__.py    console commands; debug log tee + crash reports
build_exe.py, build.bat      the two-program PyInstaller build
gamelens_app.py              entry for GameLens.exe
whisper_worker_app.py        entry for GameLensWorker.exe
start-gui.bat, run-admin.bat elevated launchers (no build needed)
```

## Ground rules for changes

- **Don't overwrite `config.yaml` or `.env`.** They hold the owner's calibrated
  subtitle box, chosen voice and API key. New settings go into
  `DEFAULTS` in `config.py`, which merges under the user's file.
- Everything printed and every native crash report goes to
  `gamelens-debug.log`. Read it before guessing.
- After changing anything in the packaged path, rebuild with `build.bat` and
  check the self-test.
- Run `tests\test_core.py` after code changes.

## Where this came from

Built in a Claude Cowork session (cloud sandbox) on 2–3 October 2026. That
session could read and write files in this folder but had no command line on
this PC and could only click — not type — on screen. That is why every test on
real hardware had to be run by hand and reported back, and why this project
moved to Claude Code, which can run `build.bat` and read the output directly.
