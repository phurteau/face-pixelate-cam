"""
face-pixelate-cam
=================
A portable virtual-camera app that pixelates ONLY faces in your webcam feed,
leaving the body and background untouched, then publishes the result to the
OBS/Streamlabs Virtual Camera so you can select it as a Video Capture Device.

Face detection uses OpenCV's built-in YuNet detector (cv2.FaceDetectorYN) -- no
MediaPipe -- so it installs with prebuilt wheels on Python 3.9 through 3.14
(including 3.13 / 3.14) with no compiler needed. YuNet needs one small model
file that ships alongside this script:

    face_detection_yunet_2023mar.onnx

Features
--------
- Face pixelation (faces only) with safety-biased tracking:
  detection runs every frame, boxes are padded, and a "hold last position"
  buffer keeps faces covered during fast motion or profile angles.
- Live lighting: brightness, contrast, saturation, warmth, gamma.
- Live hotkeys and a preview window. Settings persist to settings.json.

Run:  python pixelate_cam.py         (opens preview + virtual cam)
      python pixelate_cam.py --help  (all options)

Hotkeys (focus the preview window):
  q / ESC : quit
  [ / ]   : pixel block size  (smaller / larger blocks)
  - / =   : face padding      (less / more safety margin)
  b / B   : brightness  down / up
  c / C   : contrast    down / up
  s / S   : saturation  down / up
  w / W   : warmth      cooler / warmer
  g / G   : gamma       down / up
  h       : toggle on-screen help overlay
  p       : toggle pixelation on/off (panic peek)
  0       : reset all lighting adjustments to neutral
  5       : save settings      9 : reload settings from disk
"""

import argparse
import json
import os
import sys
import time

import numpy as np

try:
    import cv2
except ImportError:
    print("ERROR: opencv-python is not installed. Run setup.bat first.")
    sys.exit(1)

try:
    import pyvirtualcam
    from pyvirtualcam import PixelFormat
    HAVE_VCAM = True
except ImportError:
    HAVE_VCAM = False


if getattr(sys, "frozen", False):
    # Running inside a PyInstaller one-file exe: bundled data (the model) is
    # extracted to sys._MEIPASS, while settings should live next to the exe.
    BUNDLE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BUNDLE_DIR

SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
MODEL_PATH = os.path.join(BUNDLE_DIR, "face_detection_yunet_2023mar.onnx")

DEFAULTS = {
    "block": 16,          # pixelation block size in px (bigger = chunkier)
    "padding": 0.45,      # extra margin around each face box (fraction of box)
    "brightness": 0.0,    # -100..100
    "contrast": 1.0,      # 0.5..2.0
    "saturation": 1.0,    # 0.0..2.0
    "warmth": 0.0,        # -50..50 (negative cooler, positive warmer)
    "gamma": 1.0,         # 0.4..2.5
    "hold_frames": 12,    # safety: keep last face box for N frames if lost
    "min_confidence": 0.6,  # YuNet score threshold (0..1)
    "pixelate_on": True,
}

# Clamp ranges for each adjustable setting.
RANGES = {
    "block": (6, 60),
    "padding": (0.0, 1.0),
    "brightness": (-100.0, 100.0),
    "contrast": (0.5, 2.0),
    "saturation": (0.0, 2.0),
    "warmth": (-50.0, 50.0),
    "gamma": (0.4, 2.5),
}


def clamp(name, value):
    lo, hi = RANGES[name]
    return max(lo, min(hi, value))


def load_settings():
    s = dict(DEFAULTS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                s.update(json.load(f))
        except Exception as e:
            print(f"WARN: could not read settings.json ({e}); using defaults.")
    # Drop any stale keys from older versions (e.g. body-slim settings).
    return {k: s.get(k, DEFAULTS[k]) for k in DEFAULTS}


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        print("Saved settings.json")
    except Exception as e:
        print(f"WARN: could not save settings ({e})")


# ---------------------------------------------------------------------------
# Image effects
# ---------------------------------------------------------------------------

def build_gamma_lut(gamma):
    inv = 1.0 / max(0.01, gamma)
    lut = np.array([((i / 255.0) ** inv) * 255 for i in range(256)], dtype=np.uint8)
    return lut


def apply_lighting(frame, s, gamma_lut):
    """Full-frame lighting/color math. Cheap, no ML."""
    out = frame

    # Brightness (beta) + contrast (alpha) in one pass.
    alpha = float(s["contrast"])
    beta = float(s["brightness"])
    if alpha != 1.0 or beta != 0.0:
        out = cv2.convertScaleAbs(out, alpha=alpha, beta=beta)

    # Warmth: push red up / blue down (or vice versa) in BGR.
    warmth = float(s["warmth"])
    if warmth != 0.0:
        b, g, r = cv2.split(out)
        r = cv2.add(r, warmth)
        b = cv2.subtract(b, warmth)
        out = cv2.merge((b, g, r))

    # Saturation via HSV.
    sat = float(s["saturation"])
    if sat != 1.0:
        hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[..., 1] = np.clip(hsv[..., 1] * sat, 0, 255)
        out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    # Gamma via LUT.
    if float(s["gamma"]) != 1.0:
        out = cv2.LUT(out, gamma_lut)

    return out


def pixelate_region(frame, x0, y0, x1, y1, block):
    """Pixelate only the rectangle [x0:x1, y0:y1]. Returns frame."""
    h, w = frame.shape[:2]
    x0 = max(0, min(w - 1, int(x0)))
    x1 = max(0, min(w, int(x1)))
    y0 = max(0, min(h - 1, int(y0)))
    y1 = max(0, min(h, int(y1)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return frame
    roi = frame[y0:y1, x0:x1]
    rh, rw = roi.shape[:2]
    small_w = max(1, rw // block)
    small_h = max(1, rh // block)
    small = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
    pix = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
    frame[y0:y1, x0:x1] = pix
    return frame


# ---------------------------------------------------------------------------
# Face tracking with YuNet + safety-biased "hold last position"
# ---------------------------------------------------------------------------

class FaceTracker:
    def __init__(self, model_path, min_confidence, hold_frames):
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"YuNet model not found: {model_path}\n"
                "The file 'face_detection_yunet_2023mar.onnx' must sit next to "
                "pixelate_cam.py. Re-download it from the OpenCV Zoo if missing."
            )
        # Input size is set per-frame in detect(); (0,0) is a placeholder.
        self.detector = cv2.FaceDetectorYN.create(
            model_path, "", (0, 0),
            score_threshold=float(min_confidence),
            nms_threshold=0.3,
            top_k=5000,
        )
        self.hold_frames = hold_frames
        self.last_boxes = []      # list of (x0,y0,x1,y1)
        self.lost_count = 0

    def detect(self, frame_bgr, padding):
        h, w = frame_bgr.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(frame_bgr)
        boxes = []
        if faces is not None:
            for f in faces:
                # YuNet row: x, y, w, h, then 5 landmarks (10 vals), then score.
                bx, by, bw, bh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
                px = bw * padding
                py = bh * padding
                x0 = bx - px
                y0 = by - py * 1.3   # extra above for forehead/hair
                x1 = bx + bw + px
                y1 = by + bh + py
                boxes.append((x0, y0, x1, y1))

        if boxes:
            self.last_boxes = boxes
            self.lost_count = 0
            return boxes

        # No detection this frame: hold the last known boxes for a while.
        self.lost_count += 1
        if self.last_boxes and self.lost_count <= self.hold_frames:
            return self.last_boxes
        return []


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# Small clickable UI button in the preview's top-left corner (x, y, w, h).
# Clicking it toggles the info overlay. Drawn ONLY on the preview, never on the
# frame sent to the virtual camera, so your stream stays clean.
UI_BUTTON = (10, 10, 44, 32)


def point_in_button(x, y):
    bx, by, bw, bh = UI_BUTTON
    return bx <= x <= bx + bw and by <= y <= by + bh


def draw_button(frame, show_help):
    """Draw the little hide/unhide button in the corner of the preview."""
    x, y, w, h = UI_BUTTON
    overlay = frame.copy()
    cv2.rectangle(overlay, (x, y), (x + w, y + h), (35, 35, 35), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    cv2.rectangle(frame, (x, y), (x + w, y + h), (210, 210, 210), 1)
    if show_help:
        # Overlay visible -> show an "X" (click to hide the UI).
        cv2.line(frame, (x + 13, y + 10), (x + w - 13, y + h - 10), (235, 235, 235), 2, cv2.LINE_AA)
        cv2.line(frame, (x + w - 13, y + 10), (x + 13, y + h - 10), (235, 235, 235), 2, cv2.LINE_AA)
    else:
        # Overlay hidden -> show a "hamburger" (click to show the UI).
        for i in range(3):
            yy = y + 11 + i * 5
            cv2.line(frame, (x + 12, yy), (x + w - 12, yy), (235, 235, 235), 2, cv2.LINE_AA)
    return frame


def draw_help(frame, s, fps, using_vcam):
    lines = [
        f"FPS {fps:4.1f}  | pixelate:{'ON' if s['pixelate_on'] else 'OFF'}  "
        f"vcam:{'ON' if using_vcam else 'preview-only'}",
        f"block[{s['block']}] pad[{s['padding']:.2f}]  "
        f"bright[{s['brightness']:+.0f}] contr[{s['contrast']:.2f}] "
        f"sat[{s['saturation']:.2f}] warm[{s['warmth']:+.0f}] gamma[{s['gamma']:.2f}]",
        "keys: [ ] pad:- = | bBcCsSwWgG | p peek | 0 reset | 5 save | h/btn hide | q quit",
    ]
    y = 60  # start below the corner button
    for ln in lines:
        cv2.putText(frame, ln, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, ln, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0), 1, cv2.LINE_AA)
        y += 24
    return frame


def main():
    ap = argparse.ArgumentParser(description="Pixelate faces to a virtual camera.")
    ap.add_argument("--camera", type=int, default=0, help="Webcam index (default 0)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--no-vcam", action="store_true",
                    help="Preview only; do not open the virtual camera.")
    ap.add_argument("--no-preview", action="store_true",
                    help="No preview window (headless; virtual cam only).")
    ap.add_argument("--mirror", action="store_true",
                    help="Mirror the image (selfie view).")
    ap.add_argument("--clean", action="store_true",
                    help="Bare pixelated video only -- no button/overlay. Ideal "
                         "for capturing this window in OBS/Streamlabs via "
                         "'Window Capture' (no virtual camera needed).")
    args = ap.parse_args()

    s = load_settings()
    gamma_lut = build_gamma_lut(s["gamma"])

    # Face detector (fail early with a clear message if the model is missing).
    try:
        tracker = FaceTracker(MODEL_PATH, s["min_confidence"], s["hold_frames"])
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Open webcam.
    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW if os.name == "nt" else 0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    if not cap.isOpened():
        print(f"ERROR: could not open camera index {args.camera}.")
        print("Try a different --camera index (0,1,2...) or close other apps using the webcam.")
        sys.exit(1)

    ok, probe = cap.read()
    if not ok:
        print("ERROR: camera opened but returned no frame.")
        sys.exit(1)
    H, W = probe.shape[:2]
    print(f"Camera {args.camera}: {W}x{H}")

    # Virtual camera.
    vcam = None
    if not args.no_vcam:
        if not HAVE_VCAM:
            print("WARN: pyvirtualcam not installed; running preview-only.")
        else:
            try:
                vcam = pyvirtualcam.Camera(width=W, height=H, fps=args.fps,
                                           fmt=PixelFormat.BGR)
                print(f"Virtual camera: {vcam.device}")
                print(">>> In Streamlabs: add a 'Video Capture Device' and pick "
                      "'OBS Virtual Camera'. <<<")
            except Exception as e:
                print("WARN: could not start virtual camera:")
                print(f"      {e}")
                print("      This app publishes through the OBS Studio virtual")
                print("      camera. Install OBS Studio (https://obsproject.com),")
                print("      open it once, click Start Virtual Camera then Stop,")
                print("      close OBS, and re-run. (Streamlabs' own virtual cam")
                print("      is a different device and will NOT work here.)")
                print("      Running preview-only for now.")
                vcam = None

    # Guard against a no-op combo: nothing to show and nowhere to send frames.
    if args.no_preview and vcam is None:
        print("ERROR: --no-preview was set but the virtual camera is not "
              "available, so there is no output. Remove --no-preview or fix "
              "the virtual camera.")
        cap.release()
        sys.exit(1)

    WINDOW = "face-pixelate-cam (preview)"
    # Mutable UI state shared with the mouse callback. Overlay starts HIDDEN so
    # the preview is clean; only the small corner button shows.
    ui = {"show_overlay": False}

    def on_mouse(event, mx, my, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and point_in_button(mx, my):
            ui["show_overlay"] = not ui["show_overlay"]

    if not args.no_preview:
        cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(WINDOW, on_mouse)

    t_prev = time.time()
    fps = 0.0

    print("Running. Close the window (X) or press 'q' to quit.")
    if args.clean:
        print("CLEAN mode: bare video for Window Capture. Press 'h' (or click the")
        print("top-left corner) to summon the settings overlay, 'h' again to hide.")
    else:
        print("Click the corner button (or press 'h') to show/hide the overlay.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("WARN: dropped frame")
                continue
            if args.mirror:
                frame = cv2.flip(frame, 1)

            # 1) Lighting (full frame).
            frame = apply_lighting(frame, s, gamma_lut)

            # 2) Face pixelation (faces only).
            if s["pixelate_on"]:
                boxes = tracker.detect(frame, s["padding"])
                for (x0, y0, x1, y1) in boxes:
                    pixelate_region(frame, x0, y0, x1, y1, s["block"])

            # FPS meter.
            now = time.time()
            dt = now - t_prev
            t_prev = now
            if dt > 0:
                fps = 0.9 * fps + 0.1 * (1.0 / dt)

            # 3) Output to virtual cam.
            if vcam is not None:
                vcam.send(frame)
                vcam.sleep_until_next_frame()

            # 4) Preview.
            if not args.no_preview:
                disp = frame.copy()
                # Clean mode starts bare (no button, no overlay) so the captured
                # window is uncluttered. Pressing 'h' -- or clicking the top-left
                # corner -- still summons the full controls when you need them;
                # press 'h' again to go bare for streaming.
                if ui["show_overlay"]:
                    draw_help(disp, s, fps, vcam is not None)
                    draw_button(disp, ui["show_overlay"])
                elif not args.clean:
                    draw_button(disp, ui["show_overlay"])
                cv2.imshow(WINDOW, disp)
                key = cv2.waitKey(1) & 0xFF

                # Quit if the window was closed via its X button.
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
                if key in (ord('q'), 27):
                    break
                elif key == ord('h'):
                    ui["show_overlay"] = not ui["show_overlay"]
                elif key == ord('p'):
                    s["pixelate_on"] = not s["pixelate_on"]
                elif key == ord('['):
                    s["block"] = int(clamp("block", s["block"] - 2))
                elif key == ord(']'):
                    s["block"] = int(clamp("block", s["block"] + 2))
                elif key == ord('-'):
                    s["padding"] = round(clamp("padding", s["padding"] - 0.05), 2)
                elif key == ord('='):
                    s["padding"] = round(clamp("padding", s["padding"] + 0.05), 2)
                elif key == ord('b'):
                    s["brightness"] = clamp("brightness", s["brightness"] - 5)
                elif key == ord('B'):
                    s["brightness"] = clamp("brightness", s["brightness"] + 5)
                elif key == ord('c'):
                    s["contrast"] = round(clamp("contrast", s["contrast"] - 0.05), 2)
                elif key == ord('C'):
                    s["contrast"] = round(clamp("contrast", s["contrast"] + 0.05), 2)
                elif key == ord('s'):
                    s["saturation"] = round(clamp("saturation", s["saturation"] - 0.05), 2)
                elif key == ord('S'):
                    s["saturation"] = round(clamp("saturation", s["saturation"] + 0.05), 2)
                elif key == ord('w'):
                    s["warmth"] = clamp("warmth", s["warmth"] - 3)
                elif key == ord('W'):
                    s["warmth"] = clamp("warmth", s["warmth"] + 3)
                elif key == ord('g'):
                    s["gamma"] = round(clamp("gamma", s["gamma"] - 0.05), 2)
                    gamma_lut = build_gamma_lut(s["gamma"])
                elif key == ord('G'):
                    s["gamma"] = round(clamp("gamma", s["gamma"] + 0.05), 2)
                    gamma_lut = build_gamma_lut(s["gamma"])
                elif key == ord('0'):
                    # Reset only the lighting adjustments; keep pixelation setup.
                    for k in ("brightness", "contrast", "saturation", "warmth", "gamma"):
                        s[k] = DEFAULTS[k]
                    gamma_lut = build_gamma_lut(s["gamma"])
                elif key == ord('5'):   # save settings
                    save_settings(s)
                elif key == ord('9'):   # reload settings from disk
                    s = load_settings()
                    gamma_lut = build_gamma_lut(s["gamma"])
    except KeyboardInterrupt:
        print("\nInterrupted (Ctrl+C).")
    finally:
        cap.release()
        if vcam is not None:
            vcam.close()
        if not args.no_preview:
            cv2.destroyAllWindows()
        print("Stopped.")


if __name__ == "__main__":
    main()
