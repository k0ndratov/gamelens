"""Command line entry point:  python -m gamelens <command>"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import Config, default_config_path

BANNER = "GameLens - ask about the dialogue, get the answer in your headset"


def load_env(path: Path) -> None:
    """Read KEY=value lines from a .env file next to config.yaml."""
    env_file = path.parent / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# -- commands ----------------------------------------------------------------


def cmd_run(config, args) -> int:
    from .app import GameLens

    print(BANNER)
    try:
        lens = GameLens(config, api_key=args.api_key)
    except Exception as exc:
        print(f"\n  Cannot start: {exc}")
        return 1
    return lens.run()


def cmd_calibrate(config, args) -> int:
    from .calibrate import run_calibration

    print(BANNER)
    return 0 if run_calibration(config, delay=args.delay) else 1


def cmd_devices(config, args) -> int:
    from .audio import default_devices, list_devices
    from .capture import list_monitors

    print("Monitors")
    for index, mon in enumerate(list_monitors()):
        label = "all monitors combined" if index == 0 else f"monitor {index}"
        marker = " <- configured" if index == int(config.capture["monitor"]) else ""
        print(f"  {index}: {mon['width']}x{mon['height']} at "
              f"({mon['left']},{mon['top']})  {label}{marker}")

    try:
        default_in, default_out = default_devices()
        print("\nAudio devices")
        for dev in list_devices():
            kinds = []
            if dev["inputs"]:
                kinds.append(f"in:{dev['inputs']}")
            if dev["outputs"]:
                kinds.append(f"out:{dev['outputs']}")
            flags = []
            if dev["index"] == default_in:
                flags.append("default input")
            if dev["index"] == default_out:
                flags.append("default output")
            suffix = f"   [{', '.join(flags)}]" if flags else ""
            print(f"  {dev['index']:>3}: {dev['name']}  ({', '.join(kinds)}){suffix}")
        print("\n  Put the index you want in config.yaml under mic.input_device")
        print("  and voice.output_device. null means 'system default'.")
    except Exception as exc:
        print(f"\n  Could not list audio devices: {exc}")
        print("  Install the audio dependency:  pip install sounddevice")
    return 0


def cmd_voices(config, args) -> int:
    from .tts import list_voices

    try:
        voices = list_voices(args.prefix)
    except Exception as exc:
        print(f"  Could not fetch voices: {exc}")
        print("  Install it with:  pip install edge-tts")
        return 1
    current = str(config.voice["voice"])
    for voice in voices:
        marker = "  <- configured" if voice["name"] == current else ""
        print(f"  {voice['name']:<28} {voice['gender']:<8} {voice['locale']}{marker}")
    print(f"\n  {len(voices)} voices. Set voice.voice in config.yaml.")
    return 0


def cmd_doctor(config, args) -> int:
    print(BANNER)
    print("\nChecking each piece:\n")
    ok = True

    # Screen capture + OCR
    from .capture import monitor_box, sample_once
    from .ocr import build_engine

    engine, notes = build_engine(list(config.ocr["backends"]),
                                str(config.ocr["language"]))
    for note in notes:
        print(f"  ocr        {note}")
    if engine.name == "none":
        ok = False
    try:
        mon = monitor_box(int(config.capture["monitor"]))
        text, fraction, _prepared = sample_once(config, engine)
        print(f"  screen     monitor {config.capture['monitor']} is "
              f"{mon.width}x{mon.height}; subtitle box has "
              f"{fraction * 100:.2f}% text pixels")
        if text:
            print(f'  subtitle   read "{text}"')
        else:
            print("  subtitle   nothing read right now (fine if no dialogue is up)")
    except Exception as exc:
        ok = False
        print(f"  screen     FAILED: {type(exc).__name__}: {exc}")

    # Microphone
    try:
        from .audio import Recorder, describe_device

        rec = Recorder(config.mic["input_device"])
        rec.start()
        import time

        print("  mic        recording 2 seconds - say something now ...", flush=True)
        time.sleep(2.0)
        samples = rec.stop()
        level = float(abs(samples).max()) if len(samples) else 0.0
        name = describe_device(config.mic["input_device"], "microphone")
        verdict = "picked up sound" if level > 0.01 else "silent - say something and retry"
        print(f"  mic        {name}: {len(samples)} samples, peak {level:.3f} ({verdict})")
    except Exception as exc:
        ok = False
        print(f"  mic        FAILED: {type(exc).__name__}: {exc}")

    # Voice out
    try:
        from .tts import Speaker

        speaker = Speaker(config, config.voice["output_device"])
        for note in speaker.notes:
            print(f"  voice      {note}")
        print(f"  voice      backend {speaker.backend_name}")
        if speaker.available and not args.quiet:
            print("  voice      speaking a test line ...")
            speaker.say("GameLens is ready. Hold your hotkey and ask me anything.")
            if speaker.last_spoke:
                print("  voice      played - did you hear it?")
            else:
                ok = False
                print("  voice      FAILED - nothing was played")
                for err in speaker.errors:
                    print(f"  voice      {err}")
        elif not speaker.available:
            ok = False
    except Exception as exc:
        ok = False
        print(f"  voice      FAILED: {type(exc).__name__}: {exc}")

    # Whisper
    try:
        from .stt import Transcriber

        stt = Transcriber(str(config.stt["model"]), str(config.stt["device"]),
                          str(config.stt["compute_type"]),
                          cpu_threads=int(config.stt.get("cpu_threads", 0) or 0))
        print(f"  whisper    loading {config.stt['model']} (first run downloads ~75 MB, up to 2 min) ...", end=" ", flush=True)
        if stt.wait_ready(120):
            print("ready")
        else:
            ok = False
            print(f"NOT READY - {stt.status}")
            print("             If this is a download problem, the model could not be "
                  "fetched from huggingface.co (network/VPN/firewall).")
    except Exception as exc:
        ok = False
        print(f"FAILED: {exc}")

    # Claude
    try:
        from .ask import Asker

        asker = Asker(config, api_key=args.api_key)
        model = asker.resolve_model()
        for note in asker.notes:
            print(f"  claude     {note}")
        print(f"  claude     using {model}")
    except Exception as exc:
        ok = False
        print(f"  claude     FAILED: {exc}")

    print("\n  " + ("Everything looks good." if ok else
                    "Some pieces need attention - see above."))
    return 0 if ok else 1


def cmd_watch(config, args) -> int:
    """Print captured subtitles to the console without any voice involved."""
    import time

    from .buffer import DialogueBuffer
    from .capture import CaptureWorker
    from .ocr import build_engine

    engine, notes = build_engine(list(config.ocr["backends"]),
                                str(config.ocr["language"]))
    for note in notes:
        print(f"  ocr: {note}")
    buf = DialogueBuffer(float(config.buffer["minutes"]),
                         float(config.buffer["dedupe_ratio"]),
                         int(config.buffer["min_line_chars"]))

    def on_text(text: str, ts: float) -> None:
        if buf.add(text, ts):
            print(f"  {time.strftime('%H:%M:%S')}  {text}", flush=True)

    worker = CaptureWorker(config, engine, on_text=on_text, on_clear=buf.mark_gap)
    worker.start()
    print("\n  Watching the subtitle box. Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        worker.stop()
        print(f"\n  {len(buf)} lines in the buffer. Last five minutes:\n")
        print(buf.transcript() or "  (nothing)")
    return 0


def cmd_whispertest(config, args) -> int:
    """Find Whisper settings that do not crash on this PC.

    A native crash kills the whole process, so each combination is tried in its
    own child process and only the exit code is looked at.
    """
    import subprocess
    import sys
    import time

    model = str(config.stt["model"])
    # (compute_type, cpu_threads) - most capable first.
    attempts = [("int8", 0), ("int8", 1), ("int8_float32", 1),
                ("float32", 1), ("float32", 0)]
    code = (
        "import sys; from faster_whisper import WhisperModel; "
        "WhisperModel(sys.argv[1], device='cpu', compute_type=sys.argv[2], "
        "cpu_threads=int(sys.argv[3])); print('loaded')"
    )
    print(f"  Testing Whisper model '{model}' - each try runs in its own process.\n")
    winner = None
    for compute, threads in attempts:
        label = f"compute_type={compute:<13} cpu_threads={threads}"
        started = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code, model, compute, str(threads)],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            print(f"  {label}  TIMED OUT")
            continue
        took = time.time() - started
        crashed = proc.returncode in (3221225477, -1073741819, -11, 139)
        if proc.returncode == 0 and "loaded" in proc.stdout:
            print(f"  {label}  OK ({took:.1f}s)")
            winner = winner or (compute, threads)
        elif crashed:
            print(f"  {label}  CRASHED (access violation)")
        else:
            tail = (proc.stderr.strip().splitlines() or ["no output"])[-1]
            print(f"  {label}  ERROR: {tail[:110]}")

    if winner is None:
        print("\n  No setting loaded, so the Whisper engine itself does not run on")
        print("  this PC (see the messages above). Things to try, in order:")
        print("    1. winget install Microsoft.VCRedist.2015+.x64   (then run this again)")
        print("    2. .\\.venv\\Scripts\\python.exe -m pip install ctranslate2==4.4.0 "
              "faster-whisper==1.0.3")
        print("  If both fail, tell me and I will switch speech recognition to another engine.")
        return 1

    compute, threads = winner
    config.set("stt.compute_type", compute)
    config.set("stt.cpu_threads", threads)
    config.save()
    print(f"\n  Saved to config.yaml: stt.compute_type={compute}, "
          f"stt.cpu_threads={threads}")
    print("  Now run GameLens again.")
    return 0


def cmd_selftest(config, args) -> int:
    """Check that every part can be loaded - mainly for the packaged .exe."""
    import importlib

    print(f"  python {sys.version.split()[0]}, frozen={getattr(sys, 'frozen', False)}")
    ok = True
    for module in ("numpy", "PIL", "yaml", "mss", "sounddevice", "miniaudio",
                   "edge_tts", "anthropic", "winocr", "pynput.keyboard"):
        try:
            importlib.import_module(module)
            print(f"  ok      {module}")
        except Exception as exc:
            ok = False
            print(f"  FAILED  {module}: {type(exc).__name__}: {exc}")

    from .ocr import build_engine

    engine, notes = build_engine(list(config.ocr["backends"]),
                                str(config.ocr["language"]))
    print(f"  ocr     {engine.name}  ({'; '.join(notes)})")
    ok = ok and engine.name != "none"

    # The fragile part when packaged: the Whisper worker process.
    from .stt import Transcriber

    check = Transcriber("__selfcheck__")
    if check.wait_ready(120) and check._proc is not None:
        line = check._proc.stdout.readline().decode("utf-8", "replace").strip()
        print(f"  engine  {line[5:] if line.startswith('TEXT:') else line or 'no reply'}")
    else:
        ok = False
        print(f"  engine  FAILED - {check.status}")
    check.close()

    stt = Transcriber(str(config.stt["model"]), str(config.stt["device"]),
                      str(config.stt["compute_type"]),
                      cpu_threads=int(config.stt.get("cpu_threads", 0) or 0))
    ready = stt.wait_ready(240)
    print(f"  whisper {'ok' if ready else 'FAILED'} - {stt.status}")
    stt.close()
    ok = ok and ready
    print("\n  SELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def cmd_keytest(config, args) -> int:
    """Show exactly what the keyboard hook sees, and whether the combos fire."""
    import time

    from .hotkeys import make_manager

    keys = config.hotkeys
    mgr = make_manager(str(keys.get("method", "poll")))
    from .hotkeys import admin_note
    print(f"  hotkey method: {type(mgr).__name__}")
    print(f"  {admin_note()}")
    mgr.trace = lambda line: print("  " + line, flush=True)
    for name in ("talk", "repeat", "stop"):
        spec = keys.get(name)
        if spec:
            mgr.bind(
                str(spec),
                on_press=lambda n=name, sp=spec: print(
                    f"  >>>>>> HOTKEY '{n}' ({sp}) PRESSED", flush=True),
                on_release=lambda n=name: print(
                    f"  >>>>>> HOTKEY '{n}' released", flush=True),
            )
    if not mgr.start():
        print(f"  Could not start the keyboard hook: {mgr.error}")
        return 1
    print("  Press your hotkeys now (game in front is fine). Ctrl+C to quit.")
    print(f"  talk={keys.get('talk')}  repeat={keys.get('repeat')}  "
          f"stop={keys.get('stop')}\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        mgr.stop()
    return 0


def cmd_ask(config, args) -> int:
    """Ask a typed question against whatever is in the buffer right now."""
    from .ask import Asker
    from .capture import sample_once
    from .ocr import build_engine

    engine, _notes = build_engine(list(config.ocr["backends"]),
                                  str(config.ocr["language"]))
    text, _fraction, _prepared = sample_once(config, engine)
    transcript = f"[-0:00] {text}" if text else ""
    asker = Asker(config, api_key=args.api_key)
    asker.resolve_model()
    print(f"  on screen: {text or '(nothing)'}\n")
    for delta in asker.stream(transcript, args.question):
        print(delta, end="", flush=True)
    print()
    return 0


# -- argument parsing --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gamelens", description=BANNER)
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("--api-key", help="Anthropic API key (overrides the environment)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="start capturing and listening for the hotkey")
    cal = sub.add_parser("calibrate", help="draw the subtitle box with the mouse")
    cal.add_argument("--delay", type=int, default=6,
                     help="seconds to wait before the screenshot, so you can "
                          "switch to the game (default 6)")
    sub.add_parser("devices", help="list monitors, microphones and outputs")
    sub.add_parser("watch", help="print captured subtitles, no voice")
    sub.add_parser("keytest", help="show what the keyboard hook sees")
    sub.add_parser("whispertest", help="find Whisper settings that work on this PC")
    sub.add_parser("selftest", help="check every component loads (packaged exe)")

    voices = sub.add_parser("voices", help="list available speech voices")
    voices.add_argument("--prefix", default="en-", help="locale prefix filter")

    doctor = sub.add_parser("doctor", help="check every component")
    doctor.add_argument("--quiet", action="store_true",
                        help="skip the spoken test line")

    ask = sub.add_parser("ask", help="ask a typed question about the current frame")
    ask.add_argument("question", help="the question to ask")

    return parser


def setup_debug_log(path: Path):
    """Copy everything printed, and any native crash report, into a file.

    The file is what lets a problem be diagnosed without the console text being
    copied out by hand. A crash inside a native library kills the process with
    no Python traceback, so faulthandler writes its report straight to the file.
    """
    import faulthandler
    import sys
    import time

    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            path.unlink()  # keep it small; only recent runs matter
        handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        faulthandler.enable()
        return None

    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                 f"gamelens {' '.join(sys.argv[1:])} =====\n")

    class Tee:
        def __init__(self, stream):
            self._stream = stream

        def write(self, data):
            try:
                self._stream.write(data)
            except Exception:
                pass
            try:
                handle.write(data)
            except Exception:
                pass
            return len(data)

        def flush(self):
            for target in (self._stream, handle):
                try:
                    target.flush()
                except Exception:
                    pass

        def __getattr__(self, name):  # isatty, encoding, fileno, ...
            if self._stream is None:  # windowless app: no console behind us
                if name == "isatty":
                    return lambda: False
                if name == "encoding":
                    return "utf-8"
                raise AttributeError(name)
            return getattr(self._stream, name)

    sys.stdout = Tee(sys.stdout)
    sys.stderr = Tee(sys.stderr)
    faulthandler.enable(file=handle, all_threads=True)
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config) if args.config else default_config_path()
    setup_debug_log(config_path.parent / "gamelens-debug.log")
    load_env(config_path)
    config = Config.load(config_path)

    commands = {
        "run": cmd_run,
        "calibrate": cmd_calibrate,
        "devices": cmd_devices,
        "voices": cmd_voices,
        "doctor": cmd_doctor,
        "watch": cmd_watch,
        "keytest": cmd_keytest,
        "whispertest": cmd_whispertest,
        "selftest": cmd_selftest,
        "ask": cmd_ask,
    }
    handler = commands.get(args.command or "run")
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(config, args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
