# GameLens

Ask about a game's dialogue out loud and hear the answer in your headset.

GameLens is a helper for practising English while you play. During a
conversation in the game you hold a key, ask *"what did he just say?"*, and a
couple of seconds later a voice explains it in simple English. Nothing is drawn
over the game: there is a beep when it starts listening, then the answer plays.

It was built for **The Witcher 3** and works with any game that shows
subtitles.

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Using it](#using-it)
- [Building GameLens.exe](#building-gamelensexe)
- [Settings](#settings)
- [Troubleshooting](#troubleshooting)
- [Cost and privacy](#cost-and-privacy)
- [How it works](#how-it-works)
- [For developers](#for-developers)

---

## Requirements

### System

| | |
| --- | --- |
| **Operating system** | Windows 10 or 11. Subtitles are read with the OCR built into Windows. |
| **Python** | 3.10 or newer (tested on 3.12), from [python.org](https://www.python.org/downloads/). Tick **"Add python.exe to PATH"** in the installer. |
| **Microphone** | any headset or desk microphone |
| **Headphones** | recommended, so the game's microphone doesn't pick up the answer |
| **Internet** | needed for Claude's answers and the voice. Speech recognition runs offline. |
| **Disk** | about 1.5 GB for the Python packages and the speech model |
| **English OCR language** | normally installed already. If not: Settings → Time & language → Language → add **English**. |

### Accounts

- **An Anthropic API key with credit on it.** Create one at
  [console.anthropic.com](https://console.anthropic.com/settings/keys).
  API credit is billed separately from a Claude.ai Pro or Max subscription: a
  subscription does **not** give you API credit.

### Python packages

Installed automatically by `setup.bat` from [`requirements.txt`](requirements.txt):

| package | used for |
| --- | --- |
| `mss` | capturing the subtitle area of the screen |
| `Pillow`, `numpy` | image processing |
| `PyYAML` | reading `config.yaml` |
| `pynput` | keyboard hotkeys |
| `anthropic` | sending questions to Claude |
| `sounddevice` | microphone recording and audio playback |
| `faster-whisper` | speech recognition, run locally on the CPU |
| `edge-tts` | the spoken answer (free Microsoft neural voices, no key) |
| `miniaudio` | decoding the voice audio |
| `winocr` | the Windows built-in OCR |
| `pycaw`, `comtypes` | turning the game down while an answer plays (optional) |

Optional extras are listed and commented out in `requirements.txt`:
`rapidocr-onnxruntime` or `pytesseract` as alternative OCR engines, and
`pyttsx3` for an offline voice.

To build the `.exe` you also need `pyinstaller`. `build.bat` installs it.

---

## Quick start

### 1. Get the code

```bat
git clone https://github.com/<your-account>/gamelens.git
cd gamelens
```

Or download the ZIP from GitHub and unpack it.

### 2. Install

Double-click **`setup.bat`**. It:

- creates a virtual environment in `.venv`
- installs everything in `requirements.txt` (a few minutes the first time)
- creates `config.yaml` from `config.example.yaml`
- creates `.env` from `.env.example`

<details>
<summary>Doing it by hand instead</summary>

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml
copy .env.example .env
```
</details>

### 3. Add your API key

Open `.env` in Notepad and replace the placeholder with your key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

You can also paste it into the **API key** box in the GameLens window later.

> **Keep `.env` private.** It is listed in `.gitignore`, so git will not
> upload it. Don't paste your key anywhere else.

### 4. Set up the game

1. Set the game to **Borderless Window** mode. In true fullscreen, Windows
   often gives screen-capture tools a black image, and nothing gets read.
   (The Witcher 3: Options → Video → Display Mode → Borderless Window.)
2. Turn **subtitles on**. (The Witcher 3: Options → Gameplay → Subtitles.)

### 5. Calibrate the subtitle box

With the game paused on a line of dialogue, open a terminal in the project
folder and run:

```bat
.venv\Scripts\activate
python -m gamelens calibrate
```

You get 6 seconds to switch to the game. Then drag a box around the subtitle
text and press **Enter**. The box is saved to `config.yaml`, and
`calibration-crop.png` shows exactly what will be read.

### 6. Check everything

```bat
python -m gamelens doctor
```

This tests the screen capture, OCR, voice, microphone, Whisper and your API
key, then speaks a test line. If you hear it, you're ready.

### 7. Play

Double-click **`start-gui.bat`**, approve the administrator prompt, and press
**Start**.

---

## Using it

| key | what it does |
| --- | --- |
| **hold Ctrl+Alt+Q** | listens while held. Ask your question, release, and hear the answer. |
| **Ctrl+Alt+W** | explains the last subtitle line, no speaking needed |
| **Ctrl+Alt+S** | stops the answer |

Things to ask:

- "What did he just say?"
- "What are they talking about?"
- "What does *ealdorman* mean?"
- "Why is she angry with him?"
- "I missed the last minute, catch me up."

Every question includes the **last 5 minutes of subtitles** and a screenshot of
the current scene, so "what was that word he used a minute ago?" works.
Previous questions and answers are **not** remembered: every question starts
fresh.

If holding the key while you talk is awkward, set `hotkeys.mode: toggle` in
`config.yaml`. Then it's tap to start and tap to stop.

### Why it needs administrator rights

Many games run as administrator, and Windows hides an elevated window's
keystrokes from normal programs. GameLens needs admin rights so its hotkeys
work in the game. `start-gui.bat`, `run-admin.bat` and `GameLens.exe` all ask
for it.

### The window

| part | what it does |
| --- | --- |
| **Start / Stop** | starts or stops watching the screen and listening for hotkeys |
| **Subtitles** | every line as it's read, with the time |
| **Questions and answers** | what you asked and what was said back. You can also type a question here. |
| **API usage** | questions, tokens and estimated cost, for this session and in total |
| **Screen capture** | lines read, frames checked, and how many were skipped as unchanged |
| **Screenshots** | opens the folder with the last 100 subtitle strips and last 10 game frames |
| **Calibrate** | redraws the subtitle box. You get 6 seconds to switch to the game. |
| **Settings / Debug log** | opens `config.yaml` / `gamelens-debug.log` |
| **API key** | pastes or changes the key. It's saved to `.env`. |

### Ways to start it

| how | what you get |
| --- | --- |
| `start-gui.bat` | the window, run from source, as administrator |
| `dist\GameLens\GameLens.exe` or the desktop shortcut | the window, packaged (see [below](#building-gamelensexe)) |
| `run-admin.bat` | console mode, as administrator |
| `run.bat` | console mode, not elevated. Hotkeys won't reach an elevated game. |

---

## Building GameLens.exe

Run `setup.bat` first, then double-click **`build.bat`**. It takes a few
minutes and:

1. installs PyInstaller into `.venv`
2. builds two programs:
   ```
   dist\GameLens\GameLens.exe               the window (asks for administrator)
   dist\GameLens\worker\GameLensWorker.exe  speech recognition, started by the window
   ```
3. copies your `config.yaml` and `.env` next to the exe
4. creates a **GameLens** shortcut on the desktop
5. runs a self-test (approve the admin prompt). The result is in
   `dist\GameLens\gamelens-debug.log` and should end with `SELFTEST PASSED`.

The full build output is saved in `build.log`. The `dist\GameLens` folder is
self-contained and can be copied to another PC. **It includes your `.env`,
so remove that file before giving the folder to anyone.**

Run `build.bat` again after updating the code. Settings changed inside
`dist\GameLens` are kept.

---

## Settings

All settings live in `config.yaml`. `config.example.yaml` explains each one.
Anything you leave out falls back to the built-in default.

| setting | when to change it |
| --- | --- |
| `capture.text_threshold` | subtitles aren't read (lower it, e.g. 140), or junk is read from bright scenes (raise it) |
| `capture.fps` | very short lines are missed. 3 is usually enough. |
| `buffer.minutes` | how far back "catch me up" can reach |
| `stt.model` | `base.en` → `small.en` if your questions are transcribed wrongly |
| `voice.voice` | any voice from `python -m gamelens voices` |
| `voice.output_device` / `mic.input_device` | to pin your headset. Find the index with `python -m gamelens devices`. |
| `voice.duck_game_volume` | how far the game is turned down while an answer plays |
| `help.style` | `plain`, `vocab` (also explains hard words), or `russian` (gives Russian for hard words) |
| `help.level` | your English level, which sets how simple the answers are |
| `help.game` | naming the game helps with character and place names |
| `model.attach_screenshot` | `false` is cheaper and a bit faster. `true` is much better at "what's going on?" |

### Using it with another game

Only two things are game-specific: the subtitle box and how bright the text
is. Run `calibrate` in the new game, adjust `capture.text_threshold` if
needed, and set `help.game`.

### Console commands

Run these with the virtual environment active (`.venv\Scripts\activate`):

```bat
python -m gamelens run           :: the whole thing, in the console
python -m gamelens calibrate     :: set the subtitle box
python -m gamelens doctor        :: test every part
python -m gamelens devices       :: list microphones and speakers
python -m gamelens voices        :: list voices
python -m gamelens watch         :: print subtitles as they are read, no voice (good for tuning)
python -m gamelens ask "what's going on?"   :: one typed question
python -m gamelens keytest       :: show what the hotkey reader sees
python -m gamelens whispertest   :: find Whisper settings that work on this CPU
python -m gamelens selftest      :: check every part loads
```

---

## Troubleshooting

Start with `gamelens-debug.log` in the project folder (or in `dist\GameLens`
for the exe). Everything printed and any crash report goes there.

**No subtitles are captured.** This is almost always fullscreen instead of
borderless window. Run `python -m gamelens watch` to see whether lines appear.
If the box is right but nothing is read, lower `capture.text_threshold` to
about 140 and check `calibration-crop.png`.

**Garbled text.** The box probably includes part of the HUD or a name label.
Recalibrate a little tighter around the text.

**"Nothing heard."** It's using the wrong microphone. Run
`python -m gamelens devices` and set `mic.input_device`. `doctor` shows the mic
level.

**The answer plays through the speakers, not the headset.** Set
`voice.output_device` to the headset's index from `gamelens devices`.

**The hotkeys do nothing in the game.** GameLens isn't running as
administrator. Use `start-gui.bat`, `run-admin.bat` or `GameLens.exe`. Then
try `python -m gamelens keytest`.

**API errors, or "credit balance too low".** Check the key in `.env` and your
credit at [console.anthropic.com](https://console.anthropic.com).

**The first start is slow.** The Whisper speech model (about 75 MB for
`base.en`) is downloaded on first use.

---

## Cost and privacy

- **Cost:** each question is one Claude API call, usually well under one US
  cent. The screenshot is most of that. Set `model.attach_screenshot: false`
  to make questions cheaper. The window shows a running estimate.
- **What leaves your PC:** for each question, the last 5 minutes of subtitle
  text, your transcribed question and (if enabled) one screenshot go to
  Anthropic. The answer text goes to Microsoft's edge-tts service to be spoken.
- **What stays local:** your voice is turned into text on your own PC and the
  audio is never uploaded. The subtitle buffer is kept in memory only.
  The `screenshots` folder keeps the last 100 subtitle strips and 10 game
  frames for debugging. Turn it off with `screenshots.enabled: false`.
- **Your files:** `.env` (API key), `config.yaml`, `usage.json`, logs,
  screenshots and build output are all in `.gitignore` and are never
  committed.

---

## How it works

```
subtitle strip ──► threshold ──► hash ──► OCR only if changed ──► rolling 5-min buffer
                                                                          │
hold hotkey ──► mic ──► Whisper (local) ──► your question ─────────────────┤
                                                                          ▼
                                                              Claude (text, streamed)
                                                                          │
                                           headset ◄── play ◄── TTS, sentence by sentence
```

**The subtitle area is hashed before it is read.** Only bright text pixels are
kept, so the hash stays the same while a line is on screen, even though the
scenery behind it moves. One line of dialogue costs one OCR call, however many
frames it spans.

**The answer is spoken sentence by sentence.** Claude's reply is streamed, and
each finished sentence is spoken while the next one is still being written, so
speech starts about a second after you stop talking.

**Speech recognition runs in a separate process.** Whisper's engine
(CTranslate2) crashes when it's loaded into the same process as the Windows
OCR components, so it runs as its own worker, and the `.exe` is built as two
programs.

| part | library | why |
| --- | --- | --- |
| screen capture | `mss` | fast enough to grab a small region several times a second |
| OCR | Windows OCR via `winocr` | milliseconds per call, no model to download. `rapidocr` and Tesseract are fallbacks. |
| speech in | `faster-whisper` on the CPU | a short question transcribes in under a second, and the GPU stays with the game |
| speech out | `edge-tts` | natural voices, free, no key. Falls back to the Windows voice offline. |
| answers | Claude, streamed | streaming a sentence at a time keeps it feeling immediate |

---

## For developers

```
gamelens/
  app.py         the engine: start/stop, events for the window, counters
  gui.py         tkinter window. Also routes --cli / --stt-worker in the exe.
  capture.py     screen grab, threshold + hash, change detection
  ocr.py         OCR backends: windows (winocr), rapidocr, tesseract
  buffer.py      rolling dialogue buffer, fuzzy de-duplication
  ask.py         Claude prompt, streaming, sentence splitting
  stt.py         starts and talks to the Whisper worker
  stt_worker.py  the Whisper worker process
  tts.py         edge-tts, sentence pipelining, SAPI fallback, game ducking
  audio.py       mic recorder, player, volume ducking
  hotkeys.py     polling hotkeys (default) and the older hook method
  store.py       screenshots folder, pruned to fixed counts
  usage.py       token counts and estimated cost
  calibrate.py   drag-a-box subtitle calibration
  config.py      config.yaml loading, with defaults
  stdio.py       reattaches stdio in windowless processes
  __main__.py    console commands, debug log, crash reports
build_exe.py, build.bat      the two-program PyInstaller build
gamelens_app.py              entry point for GameLens.exe
whisper_worker_app.py        entry point for GameLensWorker.exe
config.example.yaml          template for config.yaml
```

Tests:

```bat
python tests\test_core.py            :: logic only, no screen, audio or network needed
python tests\test_ocr_endtoend.py    :: renders fake game frames and checks the OCR
python tests\test_gui.py             :: the window (needs a display)
```

[`CLAUDE.md`](CLAUDE.md) has detailed design notes: why each unusual choice
was made, and what breaks if it's undone.
