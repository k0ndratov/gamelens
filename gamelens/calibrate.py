"""Draw the subtitle rectangle with the mouse, then check the OCR sees it.

Run this with the game paused on a line of dialogue (windowed or borderless so
the screenshot catches it), drag a box around the subtitle area, and press
Enter. The box is saved to config.yaml and immediately tested.
"""

from __future__ import annotations

import time
import tkinter as tk

import numpy as np
from PIL import ImageTk

from .capture import (grab_monitor, monitor_box, prepare_for_ocr, text_mask,
                      to_gray)
from .ocr import build_engine

MAX_W, MAX_H = 1400, 800


def run_calibration(config, delay: int = 6) -> bool:
    monitor_index = int(config.capture["monitor"])
    mon = monitor_box(monitor_index)

    # Give the player time to click back into the game, so the screenshot is of
    # the game and not of this terminal window.
    if delay > 0:
        print(f"\n  Switch to the game NOW (Alt+Tab or click it). "
              f"Taking the screenshot in {delay} seconds.")
        print("  Make sure a subtitle line is on screen when the timer ends.\n")
        for remaining in range(delay, 0, -1):
            print(f"    {remaining} ...", flush=True)
            time.sleep(1.0)
    shot = grab_monitor(monitor_index)
    print("  Screenshot taken.")

    scale = min(MAX_W / shot.width, MAX_H / shot.height, 1.0)
    view = shot.resize((int(shot.width * scale), int(shot.height * scale)))

    root = tk.Tk()
    root.title("GameLens - drag a box around the subtitles, then press Enter")
    root.configure(bg="#111")

    photo = ImageTk.PhotoImage(view)
    canvas = tk.Canvas(root, width=view.width, height=view.height,
                       highlightthickness=0, bg="#111", cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)

    hint = tk.Label(
        root,
        text="Drag around the subtitle text.  Enter = save,  R = reset to default, "
             "Esc = cancel",
        bg="#111", fg="#ddd", pady=6,
    )
    hint.pack(fill="x")

    state: dict = {"start": None, "rect": None, "box": None, "saved": False}

    # Pre-draw the rectangle already in the config, so it is easy to adjust.
    region = config.capture["region"]
    pre = (
        region["left"] * view.width,
        region["top"] * view.height,
        (region["left"] + region["width"]) * view.width,
        (region["top"] + region["height"]) * view.height,
    )
    state["rect"] = canvas.create_rectangle(*pre, outline="#4ade80", width=2)
    state["box"] = pre

    def on_press(event) -> None:
        state["start"] = (event.x, event.y)
        canvas.coords(state["rect"], event.x, event.y, event.x, event.y)

    def on_drag(event) -> None:
        if not state["start"]:
            return
        x0, y0 = state["start"]
        canvas.coords(state["rect"], x0, y0, event.x, event.y)

    def on_release(event) -> None:
        if not state["start"]:
            return
        x0, y0 = state["start"]
        x1, y1 = event.x, event.y
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if box[2] - box[0] < 10 or box[3] - box[1] < 6:
            canvas.coords(state["rect"], *state["box"])
            return
        state["box"] = box
        canvas.coords(state["rect"], *box)
        hint.config(
            text=f"{int((box[2]-box[0])/scale)} x {int((box[3]-box[1])/scale)} px   "
                 f"Enter = save,  R = reset,  Esc = cancel"
        )

    def on_save(_event=None) -> None:
        x0, y0, x1, y1 = state["box"]
        config.set("capture.region.left", round(x0 / view.width, 4))
        config.set("capture.region.top", round(y0 / view.height, 4))
        config.set("capture.region.width", round((x1 - x0) / view.width, 4))
        config.set("capture.region.height", round((y1 - y0) / view.height, 4))
        config.save()
        state["saved"] = True
        root.destroy()

    def on_reset(_event=None) -> None:
        box = (0.15 * view.width, 0.78 * view.height,
               0.85 * view.width, 0.94 * view.height)
        state["box"] = box
        canvas.coords(state["rect"], *box)

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Return>", on_save)
    root.bind("<Escape>", lambda _e: root.destroy())
    root.bind("<r>", on_reset)
    root.bind("<R>", on_reset)
    root.focus_force()
    root.mainloop()

    if not state["saved"]:
        print("  Cancelled - nothing changed.")
        return False

    region = config.capture["region"]
    print(
        f"  Saved: left={region['left']} top={region['top']} "
        f"width={region['width']} height={region['height']}"
    )
    print(f"  Monitor {monitor_index} is {mon.width}x{mon.height}.")

    engine, notes = build_engine(list(config.ocr["backends"]),
                                str(config.ocr["language"]))
    r = config.capture["region"]
    x0 = int(r["left"] * shot.width)
    y0 = int(r["top"] * shot.height)
    x1 = x0 + max(1, int(r["width"] * shot.width))
    y1 = y0 + max(1, int(r["height"] * shot.height))
    crop = np.asarray(shot.crop((x0, y0, x1, y1)).convert("RGB"))[:, :, ::-1]
    gray = to_gray(np.dstack([crop, np.full(crop.shape[:2] + (1,), 255, np.uint8)]))
    mask = text_mask(gray, int(config.capture["text_threshold"]))
    fraction = float(mask.mean())
    prepared = prepare_for_ocr(gray, mask)
    text = engine.read(prepared) if engine.name != "none" else ""
    print(f"  OCR backend: {engine.name}")
    print(f"  Text pixels in the box: {fraction * 100:.2f}% "
          f"(threshold needs > {float(config.capture['min_text_fraction']) * 100:.2f}%)")
    if text:
        print(f'  Read back: "{text}"')
    else:
        print("  Read back: nothing. If a subtitle was on screen, try lowering")
        print("  capture.text_threshold, or widen the box slightly.")

    out = config.path.parent / "calibration-crop.png"
    try:
        prepared.save(out)
        print(f"  Wrote {out.name} so you can see exactly what the OCR gets.")
    except Exception:
        pass
    return True
