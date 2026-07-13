"""
face-pixelate-cam
=================
A portable virtual-camera app that pixelates ONLY faces in your webcam feed,
leaving the body and background untouched, then publishes the result to the
OBS/Streamlabs Virtual Camera so you can select it as a Video Capture Device.

Features
--------
- Face pixelation (faces only) with safety-biased tracking:
  detection runs every frame, boxes are padded, and a "hold last position"
  buffer keeps faces covered during fast motion or profile angles.
- Live lighting: brightness, contrast, saturation, warmth, gamma.
- Experimental body slim (off by default) using selfie segmentation + pinch warp.
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
  m       : toggle body slim (experimental)
  , / .   : slim intensity  down / up
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

import cv2
import numpy as np

try:
    import mediapipe as mp
except ImportError:
    print("ERROR: mediapipe is not installed. Run setup.bat first.")
    sys.exit(1)

try:
    import pyvirtualcam
    from pyvirtualcam import PixelFormat
    HAVE_VCAM = True
except ImportError:
    HAVE_VCAM = False


HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")

DEFAULTS = {
    "block": 16,          # pixelation block size in px (bigger = chunkier)
    "padding": 0.45,      # extra margin around each face box (fraction of box)
    "brightness": 0.0,    # -100..100
    "contrast": 1.0,      # 0.5..2.0
    "saturation": 1.0,    # 0.0..2.0
    "warmth": 0.0,        # -50..50 (negative cooler, positive warmer)
    "gamma": 1.0,         # 0.4..2.5
    "slim_on": False,
    "slim_strength": 0.12,  # 0..0.30 horizontal pinch fraction (subtle)
    "hold_frames": 12,    # safety: keep last face box for N frames if lost
    "min_confidence": 0.5,
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
    "slim_strength": (0.0, 0.30),
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
    return s


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
    """Pixelate only the rectangle [x0:x1, y0:y1] in-place-ish. Returns frame."""
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


def apply_body_slim(frame, mask, strength):
    """
    Experimental: horizontally pinch the person inward using a segmentation
    mask so the silhouette looks slimmer. Subtle by design.

    Approach: a per-row horizontal remap that squeezes sampling coordinates
    toward each row's person-centroid, but the squeeze is *localized* by a
    blurred influence mask -- full strength over the body, tapering to zero in
    the background. We blend the squeeze into the identity map (no hard mask
    composite), so the body narrows while the adjacent background gently
    stretches to fill the vacated strip. This avoids both the "double edge"
    ghost of the original silhouette and full-frame edge smearing.
    """
    if strength <= 0.0 or mask is None:
        return frame

    h, w = frame.shape[:2]
    person = (mask > 0.5).astype(np.float32)
    if person.max() <= 0.0:
        return frame  # no person detected -> nothing to slim

    # Localized influence: blur the person mask so the warp fades out into the
    # background (giving room to stretch) and is ~1 across the body interior.
    k = max(3, (w // 20) | 1)  # odd kernel ~5% of width
    infl = cv2.GaussianBlur(person, (k, k), 0)
    infl = np.clip(infl / max(infl.max(), 1e-6), 0.0, 1.0)

    xs = np.tile(np.arange(w, dtype=np.float32), (h, 1))
    # Per-row centroid of the person; fall back to frame center where empty.
    row_sum = person.sum(axis=1)
    col_idx = np.arange(w, dtype=np.float32)
    cx = np.where(
        row_sum > 0,
        (person * col_idx).sum(axis=1) / np.maximum(row_sum, 1e-6),
        w / 2.0,
    ).astype(np.float32)[:, None]

    # map_x = identity where infl=0, squeeze (slope 1+strength) where infl=1.
    # Sampling further from center makes the rendered body appear narrower,
    # and the taper lets the background stretch in instead of leaving a ghost.
    map_x = xs + (strength * infl) * (xs - cx)
    map_y = np.tile(np.arange(h, dtype=np.float32)[:, None], (1, w))

    out = cv2.remap(
        frame, map_x, map_y, interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return out



# ---------------------------------------------------------------------------
# Face tracking with safety-biased "hold last position"
# ---------------------------------------------------------------------------

class FaceTracker:
    def __init__(self, min_confidence, hold_frames):
        self.detector = mp.solutions.face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=min_confidence
        )
        self.hold_frames = hold_frames
        self.last_boxes = []      # list of (x0,y0,x1,y1)
        self.lost_count = 0

    def detect(self, frame_bgr, padding):
        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        res = self.detector.process(rgb)
        boxes = []
        if res.detections:
            for det in res.detections:
                rb = det.location_data.relative_bounding_box
                bx = rb.xmin * w
                by = rb.ymin * h
                bw = rb.width * w
                bh = rb.height * h
                # Safety padding around the detected face.
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

def draw_help(frame, s, fps, using_vcam):
    lines = [
        f"FPS {fps:4.1f}  | pixelate:{'ON' if s['pixelate_on'] else 'OFF'}  "
        f"vcam:{'ON' if using_vcam else 'preview-only'}",
        f"block[{s['block']}] pad[{s['padding']:.2f}]  "
        f"bright[{s['brightness']:+.0f}] contr[{s['contrast']:.2f}] "
        f"sat[{s['saturation']:.2f}] warm[{s['warmth']:+.0f}] gamma[{s['gamma']:.2f}]",
        f"slim:{'ON' if s['slim_on'] else 'off'}[{s['slim_strength']:.2f}]   "
        f"keys: [ ] pad:- = | bBcCsSwWgG | m , . | p peek | 0 reset | 5 save | q quit",
    ]
    y = 22
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
    args = ap.parse_args()

    s = load_settings()
    gamma_lut = build_gamma_lut(s["gamma"])

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

    tracker = FaceTracker(s["min_confidence"], s["hold_frames"])
    segmenter = mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=1)

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
                print("      Is the OBS/Streamlabs Virtual Camera driver installed?")
                print("      Running preview-only for now.")
                vcam = None

    show_help = True
    t_prev = time.time()
    fps = 0.0

    # Guard against a no-op combo: nothing to show and nowhere to send frames.
    if args.no_preview and vcam is None:
        print("ERROR: --no-preview was set but the virtual camera is not "
              "available, so there is no output. Remove --no-preview or fix "
              "the virtual camera.")
        cap.release()
        sys.exit(1)

    print("Running. Focus the preview window and press 'h' for help, 'q' to quit.")
    while True:
        ok, frame = cap.read()
        if not ok:
            print("WARN: dropped frame")
            continue
        if args.mirror:
            frame = cv2.flip(frame, 1)

        # 1) Lighting (full frame).
        frame = apply_lighting(frame, s, gamma_lut)

        # 2) Body slim (experimental, optional).
        if s["slim_on"] and s["slim_strength"] > 0:
            seg = segmenter.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            frame = apply_body_slim(frame, seg.segmentation_mask, s["slim_strength"])

        # 3) Face pixelation (faces only) -- done AFTER slim so faces stay covered.
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

        # 4) Output to virtual cam.
        if vcam is not None:
            vcam.send(frame)
            vcam.sleep_until_next_frame()

        # 5) Preview.
        if not args.no_preview:
            disp = frame.copy()
            if show_help:
                draw_help(disp, s, fps, vcam is not None)
            cv2.imshow("face-pixelate-cam (preview)", disp)
            key = cv2.waitKey(1) & 0xFF
            if key == 255:
                continue
            if key in (ord('q'), 27):
                break
            elif key == ord('h'):
                show_help = not show_help
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
            elif key == ord('m'):
                s["slim_on"] = not s["slim_on"]
            elif key == ord(','):
                s["slim_strength"] = round(clamp("slim_strength", s["slim_strength"] - 0.02), 2)
            elif key == ord('.'):
                s["slim_strength"] = round(clamp("slim_strength", s["slim_strength"] + 0.02), 2)
            elif key == ord('0'):
                # Reset only the lighting adjustments; keep pixelation/slim setup.
                for k in ("brightness", "contrast", "saturation", "warmth", "gamma"):
                    s[k] = DEFAULTS[k]
                gamma_lut = build_gamma_lut(s["gamma"])
            elif key == ord('5'):   # save settings
                save_settings(s)
            elif key == ord('9'):   # reload settings from disk
                s = load_settings()
                gamma_lut = build_gamma_lut(s["gamma"])

    cap.release()
    if vcam is not None:
        vcam.close()
    if not args.no_preview:
        cv2.destroyAllWindows()
    print("Stopped.")


if __name__ == "__main__":
    main()
