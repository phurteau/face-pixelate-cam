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
  t       : toggle the theme / accent color picker (HSV wheel)
  p       : toggle pixelation on/off (panic peek)
  0       : reset all lighting adjustments to neutral
  5       : save settings      9 : reload settings from disk
  U       : download update (shown only when a newer version is available)
  N       : dismiss the update banner
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
import webbrowser

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

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


# ---------------------------------------------------------------------------
# Professional text rendering via a real TrueType font (Pillow). Falls back to
# OpenCV's cleaner DUPLEX Hershey font if Pillow or the font is unavailable.
# Text is rendered to small RGBA patches and alpha-composited onto the frame,
# so only the text region is touched (no full-frame conversion per string).
# ---------------------------------------------------------------------------

# Preferred fonts, in order. Segoe UI ships on every Windows machine.
_FONT_FILES = {
    False: [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"],
    True:  [r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeui.ttf"],
}
_FONT_CACHE = {}


def _load_font(size, bold=False):
    if not HAVE_PIL:
        return None
    key = (int(size), bool(bold))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    for path in _FONT_FILES[bool(bold)]:
        try:
            font = ImageFont.truetype(path, int(size))
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
    _FONT_CACHE[key] = font
    return font


def text_width(text, size=16, bold=False):
    """Pixel width of `text` in the current font (for layout/right-align)."""
    font = _load_font(size, bold)
    if font is None:
        return int(len(text) * size * 0.55)
    l, _, r, _ = font.getbbox(text)
    return int(r - l)


_TEXT_CACHE = {}


def _render_text_patch(text, size, bold, color_bgr, outline):
    """Render text to a cached BGRA patch (B,G,R,alpha as float32 0..1 alpha).

    Cached by content so steady-state overlays only pay compositing cost, not
    re-rasterization. Returns (bgr_float, alpha) or None if PIL unavailable.
    """
    font = _load_font(size, bold)
    if font is None:
        return None
    key = (text, int(size), bool(bold), tuple(color_bgr), int(outline))
    cached = _TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    l, t, r, b = font.getbbox(text, stroke_width=outline)
    pad = outline + 1
    pw, ph = (r - l) + pad * 2, (b - t) + pad * 2
    img = Image.new("RGBA", (max(1, pw), max(1, ph)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = (color_bgr[2], color_bgr[1], color_bgr[0], 255)          # BGR -> RGB
    draw.text((pad - l, pad - t), text, font=font, fill=fill,
              stroke_width=outline, stroke_fill=(0, 0, 0, 210))
    arr = np.asarray(img)
    bgr = arr[..., 2::-1].astype(np.float32)                        # RGB -> BGR
    alpha = arr[..., 3:4].astype(np.float32) / 255.0
    out = (bgr, alpha)
    _TEXT_CACHE[key] = out
    if len(_TEXT_CACHE) > 160:
        _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
    return out


def blit_text(frame, text, xy, color_bgr, size=16, bold=False, outline=2):
    """Draw `text` at top-left `xy` onto the BGR `frame` in-place (TTF)."""
    if not text:
        return
    patch = _render_text_patch(text, size, bold, color_bgr, outline)
    if patch is None:
        # Hershey fallback (DUPLEX is noticeably cleaner than SIMPLEX).
        scale = size / 30.0
        base = (int(xy[0]), int(xy[1]) + int(size * 0.9))
        if outline:
            cv2.putText(frame, text, base, cv2.FONT_HERSHEY_DUPLEX, scale,
                        (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, text, base, cv2.FONT_HERSHEY_DUPLEX, scale,
                    color_bgr, 1, cv2.LINE_AA)
        return
    bgr, alpha = patch
    ph, pw = bgr.shape[:2]
    x, y = int(xy[0]), int(xy[1])
    fh, fw = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + pw), min(fh, y + ph)
    if x1 <= x0 or y1 <= y0:
        return
    sx0, sy0 = x0 - x, y0 - y
    a = alpha[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
    c = bgr[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
    roi = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (c * a + roi * (1 - a)).astype(np.uint8)


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
LOG_PATH = os.path.join(APP_DIR, "run-log.txt")


def fatal(msg):
    """Report a fatal startup error even when there is no console window.

    When launched via pythonw.exe (windowless), stdout/stderr are invisible, so
    we (1) write the message to run-log.txt next to the app and (2) pop a native
    Windows message box. Then exit non-zero.
    """
    print(f"ERROR: {msg}")
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("face-pixelate-cam could not start:\n\n" + msg + "\n")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, msg, "face-pixelate-cam - cannot start", 0x10)
        except Exception:
            pass
    sys.exit(1)


# ---------------------------------------------------------------------------
# Auto-update: check GitHub Releases for a newer version and offer a one-click
# download. Runs in a background thread; failures (offline, rate-limited) are
# silently ignored so they never disrupt streaming.
# ---------------------------------------------------------------------------

APP_VERSION = "1.3.0"
REPO = "phurteau/face-pixelate-cam"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
# How long the banner stays on screen (seconds) before auto-hiding, so it never
# lingers on a Window-Capture stream.
UPDATE_BANNER_SECONDS = 20


def parse_version(text):
    """Extract a comparable (major, minor, patch) tuple from e.g. 'v1.2.3'."""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3])


def is_newer(latest, current):
    """True if version string `latest` is strictly newer than `current`."""
    lv, cv = parse_version(latest), parse_version(current)
    if not lv:
        return False
    n = max(len(lv), len(cv))
    lv = lv + (0,) * (n - len(lv))
    cv = cv + (0,) * (n - len(cv))
    return lv > cv


def fetch_latest_release(timeout=10):
    """Query the GitHub Releases API. Returns {tag, html_url, zip_url} or None."""
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "face-pixelate-cam-updater",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    html_url = data.get("html_url", RELEASES_PAGE)
    zip_url = None
    for a in data.get("assets", []):
        if str(a.get("name", "")).lower().endswith(".zip"):
            zip_url = a.get("browser_download_url")
            break
    if not zip_url:
        zip_url = data.get("zipball_url")  # fallback: source zip
    return {"tag": tag, "html_url": html_url, "zip_url": zip_url}


def start_update_check(state):
    """Kick off the version check in a daemon thread. Fills state['update']."""
    def worker():
        try:
            info = fetch_latest_release()
            if info and is_newer(info.get("tag", ""), APP_VERSION):
                state["update"] = info
        except Exception:
            pass  # offline / rate-limited / API change -> just skip silently
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def download_update(info, state):
    """Download the release zip to the user's Downloads folder and reveal it.

    Runs in its own thread so the video never freezes. Updates state['status']
    with progress; falls back to opening the Releases page in a browser on any
    error.
    """
    import shutil
    import subprocess

    def status(msg):
        state["status"] = msg

    try:
        zip_url = info.get("zip_url")
        if not zip_url:
            raise ValueError("no download URL in release")
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(downloads):
            downloads = os.path.expanduser("~")
        tag = (info.get("tag") or "latest").lstrip("v")
        dest = os.path.join(downloads, f"face-pixelate-cam-{tag}.zip")

        status("Downloading update...")
        req = urllib.request.Request(
            zip_url, headers={"User-Agent": "face-pixelate-cam-updater"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)

        state["downloaded_path"] = dest
        status(f"Downloaded to {dest} - opening folder...")
        if os.name == "nt":
            try:
                subprocess.Popen(["explorer", "/select,", dest])
            except Exception:
                pass
        status(f"Saved to Downloads: face-pixelate-cam-{tag}.zip "
               "(extract it and run setup.bat)")
    except Exception as e:
        status(f"Download failed ({e}); opening Releases page...")
        try:
            webbrowser.open(info.get("html_url", RELEASES_PAGE))
        except Exception:
            pass


def start_download(info, state):
    if state.get("downloading"):
        return
    state["downloading"] = True
    t = threading.Thread(target=download_update, args=(info, state), daemon=True)
    t.start()
    return t


DEFAULTS = {
    "block": 16,          # pixelation block size in px (bigger = chunkier)
    "padding": 0.45,      # extra margin around each face box (fraction of box)
    "brightness": 0.0,    # -100..100
    "contrast": 1.0,      # 0.5..2.0
    "saturation": 1.0,    # 0.0..2.0
    "warmth": 0.0,        # -50..50 (negative cooler, positive warmer)
    "gamma": 1.0,         # 0.4..2.5
    "hold_frames": 20,    # safety: keep last face box for N frames if lost
    "min_confidence": 0.5,  # YuNet score threshold (0..1); lower = catch more
    "pixelate_on": True,
    "theme": "dark",       # "dark" (default) or "light"
    "accent": "#025500",   # single accent that drives all UI highlights
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
# Theme / design tokens (dark default + light) driven by a single accent.
# Faithful port of the web token system: every drawn color comes from a token,
# and one user-chosen ACCENT drives all highlights. Colors are stored as hex
# and resolved to BGR for OpenCV. acc2 / acc-ink are DERIVED from the accent.
# ---------------------------------------------------------------------------

DEFAULT_ACCENT = "#025500"

THEME_TOKENS = {
    # neutral surfaces only -- no color tint, so any accent looks good
    "dark": {
        "bg": "#000000", "bg2": "#060606", "panel": "#101012", "panel2": "#17171a",
        "line": "#2a2a2e", "txt": "#ededed", "dim": "#9a9a9a",
    },
    "light": {
        "bg": "#eef4ef", "bg2": "#e6ede8", "panel": "#ffffff", "panel2": "#f2f7f3",
        "line": "#cfe0d4", "txt": "#12251a", "dim": "#5c7a66",
    },
}


def hex_to_rgb(h):
    h = str(h).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        h = "025500"
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (2, 85, 0)


def rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, int(round(v)))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_bgr(h):
    r, g, b = hex_to_rgb(h)
    return (b, g, r)  # OpenCV is BGR


def rgb_to_hsl(r, g, b):
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    d = mx - mn
    if d == 0:
        return 0.0, 0.0, l
    s = d / (1 - abs(2 * l - 1)) if l not in (0, 1) else 0.0
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60.0, s, l


def hsl_to_rgb(h, s, l):
    c = (1 - abs(2 * l - 1)) * s
    hp = (h % 360) / 60.0
    x = c * (1 - abs(hp % 2 - 1))
    if hp < 1:   r, g, b = c, x, 0
    elif hp < 2: r, g, b = x, c, 0
    elif hp < 3: r, g, b = 0, c, x
    elif hp < 4: r, g, b = 0, x, c
    elif hp < 5: r, g, b = x, 0, c
    else:        r, g, b = c, 0, x
    m = l - c / 2
    return ((r + m) * 255, (g + m) * 255, (b + m) * 255)


def derive_accent(acc_hex):
    """Derive the accent companion tokens exactly as the design spec dictates.

    - acc2   : same hue, saturation raised to >=45%, lightness +20% (cap 75%).
    - acc-ink: black ink on light accents (YIQ > 140), white otherwise.
    Returns dict of BGR tuples plus the canonical hex strings.
    """
    r, g, b = hex_to_rgb(acc_hex)
    h, s, l = rgb_to_hsl(r, g, b)
    s2 = max(s, 0.45)
    l2 = min(l + 0.20, 0.75)
    acc2_rgb = hsl_to_rgb(h, s2, l2)
    yiq = (r * 299 + g * 587 + b * 114) / 1000.0
    ink_hex = "#08140a" if yiq > 140 else "#ffffff"
    return {
        "acc": (b, g, r),
        "acc2": (int(acc2_rgb[2]), int(acc2_rgb[1]), int(acc2_rgb[0])),
        "acc_ink": hex_to_bgr(ink_hex),
        "acc_hex": rgb_to_hex((r, g, b)),
        "acc2_hex": rgb_to_hex(acc2_rgb),
    }


def build_theme(theme_name, accent_hex):
    """Resolve all tokens to BGR for the current theme + accent."""
    base = THEME_TOKENS.get(theme_name, THEME_TOKENS["dark"])
    th = {k: hex_to_bgr(v) for k, v in base.items()}
    th.update(derive_accent(accent_hex))
    th["name"] = theme_name
    return th


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
        # Reflect-border fraction added around the frame before detection so
        # faces cut off at the edge are still found.
        self.detect_pad = 0.25
        # How close (fraction of face size) a face must be to an edge before we
        # extend its pixelation box out past that edge.
        self.edge_margin = 0.15

    def detect(self, frame_bgr, padding):
        h, w = frame_bgr.shape[:2]

        # Reflect-pad the frame before detection. A face that is half cut off at
        # the frame edge becomes a more complete face in the mirrored border, so
        # YuNet can still detect it (this is the key fix for edge faces that
        # would otherwise go undetected and expose the face).
        bx_pad = int(round(w * self.detect_pad))
        by_pad = int(round(h * self.detect_pad))
        padded = cv2.copyMakeBorder(
            frame_bgr, by_pad, by_pad, bx_pad, bx_pad, cv2.BORDER_REFLECT)
        ph, pw = padded.shape[:2]
        self.detector.setInputSize((pw, ph))
        _, faces = self.detector.detect(padded)

        boxes = []
        if faces is not None:
            for f in faces:
                # YuNet row: x, y, w, h, then 5 landmarks (10 vals), then score.
                # Map from padded coords back to the original frame.
                bx = float(f[0]) - bx_pad
                by = float(f[1]) - by_pad
                bw = float(f[2])
                bh = float(f[3])

                # Keep only faces whose center lands within (or right at) the
                # real frame. This discards pure mirror-reflection detections
                # that live entirely in the border, while keeping genuine
                # half-off-the-edge faces (whose center sits near the edge).
                cx = bx + bw / 2.0
                cy = by + bh / 2.0
                if not (-0.15 * bw <= cx <= w + 0.15 * bw and
                        -0.15 * bh <= cy <= h + 0.15 * bh):
                    continue

                px = bw * padding
                py = bh * padding
                x0 = bx - px
                y0 = by - py * 1.3   # extra above for forehead/hair
                x1 = bx + bw + px
                y1 = by + bh + py

                # Edge-glue: if a face is near a frame edge, extend the covered
                # box past that edge so a face sliding off-frame never leaves an
                # exposed sliver at the very border.
                m_x = bw * self.edge_margin
                m_y = bh * self.edge_margin
                if bx <= m_x:
                    x0 = -px - m_x
                if by <= m_y:
                    y0 = -py - m_y
                if bx + bw >= w - m_x:
                    x1 = w + px + m_x
                if by + bh >= h - m_y:
                    y1 = h + py + m_y

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


def draw_button(frame, show_help, th):
    """Corner show/hide button. Neutral panel surface; active state fills accent."""
    x, y, w, h = UI_BUTTON
    overlay = frame.copy()
    fill = th["acc"] if show_help else th["panel2"]
    cv2.rectangle(overlay, (x, y), (x + w, y + h), fill, -1)
    cv2.addWeighted(overlay, 0.85, frame, 0.15, 0, frame)
    border = th["acc2"] if show_help else th["line"]
    cv2.rectangle(frame, (x, y), (x + w, y + h), border, 1)
    icon = th["acc_ink"] if show_help else th["txt"]
    if show_help:
        cv2.line(frame, (x + 13, y + 10), (x + w - 13, y + h - 10), icon, 2, cv2.LINE_AA)
        cv2.line(frame, (x + w - 13, y + 10), (x + 13, y + h - 10), icon, 2, cv2.LINE_AA)
    else:
        for i in range(3):
            yy = y + 11 + i * 5
            cv2.line(frame, (x + 12, yy), (x + w - 12, yy), icon, 2, cv2.LINE_AA)
    return frame


def draw_update_banner(frame, text, th):
    """Bottom update banner styled as a primary/accent call-to-action.

    Drawn on the preview copy only (never on the virtual-camera output), so it
    is stream-safe. In Window-Capture (clean) mode it auto-hides.
    """
    h, w = frame.shape[:2]
    bar_h = 34
    y0 = h - bar_h
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, y0), (w, h), th["acc"], -1)      # primary fill
    cv2.addWeighted(overlay, 0.88, frame, 0.12, 0, frame)
    cv2.line(frame, (0, y0), (w, y0), th["acc2"], 1)           # accent top edge
    cv2.circle(frame, (18, y0 + bar_h // 2), 5, th["acc2"], -1, cv2.LINE_AA)
    blit_text(frame, text, (34, y0 + 9), th["acc_ink"], size=16, outline=0)
    return frame


def draw_help(frame, s, fps, using_vcam, th):
    lines = [
        (f"face-pixelate-cam  v{APP_VERSION}", th["acc2"], True, 17),
        (f"pixelate {'ON' if s['pixelate_on'] else 'OFF'}   "
         f"vcam {'ON' if using_vcam else 'preview'}   {fps:2.0f} fps",
         th["dim"], False, 15),
        (f"block {s['block']}   pad {s['padding']:.2f}   "
         f"bright {s['brightness']:+.0f}   contrast {s['contrast']:.2f}   "
         f"sat {s['saturation']:.2f}   warm {s['warmth']:+.0f}   "
         f"gamma {s['gamma']:.2f}", th["txt"], False, 16),
        ("[ ] size    - = padding    b c s w g lighting    "
         "p peek    0 reset    5 save    t theme", th["dim"], False, 14),
    ]
    pad = 14
    x0 = 8
    y = 50
    row_h = 26
    widths = [text_width(t, sz, bold) for (t, _, bold, sz) in lines]
    x1 = min(frame.shape[1] - 8, x0 + max(widths) + pad * 2)
    y_top = y - 10
    y_bot = y + row_h * len(lines) + 6
    overlay = frame.copy()
    cv2.rectangle(overlay, (x0, y_top), (x1, y_bot), th["panel"], -1)
    cv2.addWeighted(overlay, 0.78, frame, 0.22, 0, frame)
    cv2.rectangle(frame, (x0, y_top), (x1, y_bot), th["line"], 1)
    cy = y
    for (txt, color, bold, sz) in lines:
        blit_text(frame, txt, (x0 + pad, cy), color, size=sz, bold=bold)
        cy += row_h
    return frame


# ---------------------------------------------------------------------------
# HSV color-wheel accent picker (the design system's signature control).
# hue = angle, saturation = radius, a Brightness (Value) bar below the wheel,
# a live swatch + hex readout, a theme toggle and a reset button.
# ---------------------------------------------------------------------------

def rgb_to_hsv(r, g, b):
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        h = 0.0
    elif mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    h *= 60.0
    s = 0.0 if mx == 0 else d / mx
    return h, s, mx


def hsv_to_rgb(h, s, v):
    hp = (h % 360) / 60.0
    c = v * s
    x = c * (1 - abs(hp % 2 - 1))
    if hp < 1:   r, g, b = c, x, 0
    elif hp < 2: r, g, b = x, c, 0
    elif hp < 3: r, g, b = 0, c, x
    elif hp < 4: r, g, b = 0, x, c
    elif hp < 5: r, g, b = x, 0, c
    else:        r, g, b = c, 0, x
    m = v - c
    return ((r + m) * 255, (g + m) * 255, (b + m) * 255)


_WHEEL_CACHE = {}


def render_color_wheel(radius, value):
    """Return (bgr_image, circle_mask) for an HSV wheel at the given Value.

    Cached by (radius, quantized value) so it is only recomputed on change.
    hue = angle around the circle, saturation = distance from the center.
    """
    key = (radius, int(round(value * 50)))
    cached = _WHEEL_CACHE.get(key)
    if cached is not None:
        return cached
    size = radius * 2
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    dx = xx - radius
    dy = yy - radius
    dist = np.sqrt(dx * dx + dy * dy)
    mask = dist <= radius
    ang = (np.degrees(np.arctan2(-dy, dx)) % 360.0)
    hsv = np.zeros((size, size, 3), np.uint8)
    hsv[..., 0] = (ang / 2.0).astype(np.uint8)                 # OpenCV hue 0-179
    hsv[..., 1] = np.clip(dist / radius * 255.0, 0, 255).astype(np.uint8)
    hsv[..., 2] = int(round(max(0.0, min(1.0, value)) * 255))
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    bgr[~mask] = 0
    _WHEEL_CACHE[key] = (bgr, mask)
    if len(_WHEEL_CACHE) > 24:
        _WHEEL_CACHE.pop(next(iter(_WHEEL_CACHE)))
    return bgr, mask


_BAR_CACHE = {}


def render_brightness_bar(hue, sat, bw, bh):
    """Vectorized + cached Value ramp strip for the given hue/saturation."""
    key = (int(round(hue)), int(round(sat * 50)), bw, bh)
    cached = _BAR_CACHE.get(key)
    if cached is not None:
        return cached
    vals = (np.linspace(0, 1, bw, dtype=np.float32) * 255).astype(np.uint8)
    row = np.zeros((1, bw, 3), np.uint8)
    row[0, :, 0] = int(round((hue % 360) / 2.0))          # OpenCV hue 0-179
    row[0, :, 1] = int(round(max(0.0, min(1.0, sat)) * 255))
    row[0, :, 2] = vals
    strip = cv2.cvtColor(row, cv2.COLOR_HSV2BGR)
    strip = np.repeat(strip, bh, axis=0)
    _BAR_CACHE[key] = strip
    if len(_BAR_CACHE) > 48:
        _BAR_CACHE.pop(next(iter(_BAR_CACHE)))
    return strip


def picker_geometry(frame_w, frame_h):
    """Compute the picker panel layout for the current frame size."""
    pw, ph = 210, 356
    px = frame_w - pw - 20
    py = 50
    R = 78
    cx = px + pw // 2
    cy = py + 34 + R
    bar = (px + 16, cy + R + 18, pw - 32, 14)
    swatch = (px + 16, bar[1] + bar[3] + 14, 40, 24)
    btn_y = py + ph - 40
    theme_btn = (px + 16, btn_y, 88, 26)
    reset_btn = (px + pw - 16 - 88, btn_y, 88, 26)
    return {
        "panel": (px, py, pw, ph), "wheel": (cx, cy, R), "bar": bar,
        "swatch": swatch, "theme_btn": theme_btn, "reset_btn": reset_btn,
    }


def draw_picker(frame, s, th, geom):
    """Draw the accent picker panel. Geometry comes from picker_geometry()."""
    px, py, pw, ph = geom["panel"]
    cx, cy, R = geom["wheel"]
    overlay = frame.copy()
    cv2.rectangle(overlay, (px, py), (px + pw, py + ph), th["panel"], -1)
    cv2.addWeighted(overlay, 0.9, frame, 0.1, 0, frame)
    cv2.rectangle(frame, (px, py), (px + pw, py + ph), th["line"], 1)
    blit_text(frame, "Accent", (px + 16, py + 12), th["txt"], size=17, bold=True)

    r, g, b = hex_to_rgb(s["accent"])
    h, sat, val = rgb_to_hsv(r, g, b)

    # Wheel (composited within its circular mask).
    wheel, mask = render_color_wheel(R, val)
    y0, y1 = cy - R, cy + R
    x0, x1 = cx - R, cx + R
    roi = frame[y0:y1, x0:x1]
    roi[mask] = wheel[mask]
    cv2.circle(frame, (cx, cy), R, th["line"], 1, cv2.LINE_AA)
    # Selection dot.
    dot = (int(cx + np.cos(np.radians(h)) * sat * R),
           int(cy - np.sin(np.radians(h)) * sat * R))
    cv2.circle(frame, dot, 6, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.circle(frame, dot, 6, (0, 0, 0), 1, cv2.LINE_AA)

    # Brightness (Value) bar -- vectorized + cached (only re-rendered when the
    # hue/saturation change), then blitted as a strip.
    bx, by, bw, bh = geom["bar"]
    strip = render_brightness_bar(h, sat, bw, bh)
    frame[by:by + bh, bx:bx + bw] = strip
    cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), th["line"], 1)
    hx = bx + int(val * bw)
    cv2.rectangle(frame, (hx - 2, by - 2), (hx + 2, by + bh + 2), th["txt"], -1)
    blit_text(frame, "Brightness", (bx, by - 20), th["dim"], size=12)

    # Swatch + hex readout.
    sx, sy, sw, sh = geom["swatch"]
    cv2.rectangle(frame, (sx, sy), (sx + sw, sy + sh), th["acc"], -1)
    cv2.rectangle(frame, (sx, sy), (sx + sw, sy + sh), th["line"], 1)
    blit_text(frame, s["accent"].upper(), (sx + sw + 12, sy + 4), th["txt"], size=16)

    # Theme + Reset buttons.
    for key, label in (("theme_btn", f"Theme: {s['theme']}"),
                       ("reset_btn", "Reset")):
        bxx, byy, bww, bhh = geom[key]
        cv2.rectangle(frame, (bxx, byy), (bxx + bww, byy + bhh), th["panel2"], -1)
        cv2.rectangle(frame, (bxx, byy), (bxx + bww, byy + bhh), th["line"], 1)
        lw = text_width(label, 13)
        blit_text(frame, label, (bxx + max(6, (bww - lw) // 2), byy + 6),
                  th["txt"], size=13)
    return frame


def picker_click(s, geom, mx, my, dragging):
    """Apply a click/drag inside the picker. Returns True if it hit a control."""
    cx, cy, R = geom["wheel"]
    r, g, b = hex_to_rgb(s["accent"])
    h, sat, val = rgb_to_hsv(r, g, b)

    dx, dy = mx - cx, my - cy
    dist = (dx * dx + dy * dy) ** 0.5
    if dist <= R + 6:
        new_h = np.degrees(np.arctan2(-dy, dx)) % 360.0
        new_s = min(1.0, dist / R)
        col = hsv_to_rgb(new_h, new_s, val if val > 0 else 1.0)
        s["accent"] = rgb_to_hex(col)
        return True

    bx, by, bw, bh = geom["bar"]
    if bx - 4 <= mx <= bx + bw + 4 and by - 6 <= my <= by + bh + 6:
        new_v = max(0.0, min(1.0, (mx - bx) / bw))
        col = hsv_to_rgb(h, sat, new_v)
        s["accent"] = rgb_to_hex(col)
        return True

    if not dragging:
        tx, ty, tw, thh = geom["theme_btn"]
        if tx <= mx <= tx + tw and ty <= my <= ty + thh:
            s["theme"] = "light" if s["theme"] == "dark" else "dark"
            return True
        rx, ry, rw, rh = geom["reset_btn"]
        if rx <= mx <= rx + rw and ry <= my <= ry + rh:
            s["accent"] = DEFAULT_ACCENT
            return True
    return False


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
    ap.add_argument("--no-update-check", action="store_true",
                    help="Do not check GitHub for a newer version on launch.")
    ap.add_argument("--accent", default=None,
                    help="Set the accent color as #rrggbb (persists to settings).")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="Set the UI theme (persists to settings).")
    ap.add_argument("--version", action="version",
                    version=f"face-pixelate-cam {APP_VERSION}")
    args = ap.parse_args()

    s = load_settings()
    if args.accent:
        s["accent"] = args.accent
    if args.theme:
        s["theme"] = args.theme
    if args.accent or args.theme:
        save_settings(s)  # CLI overrides persist immediately
    theme = build_theme(s["theme"], s["accent"])
    gamma_lut = build_gamma_lut(s["gamma"])

    # Face detector (fail early with a clear message if the model is missing).
    try:
        tracker = FaceTracker(MODEL_PATH, s["min_confidence"], s["hold_frames"])
    except FileNotFoundError as e:
        fatal(str(e))

    # Open webcam.
    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW if os.name == "nt" else 0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    if not cap.isOpened():
        fatal(f"Could not open camera index {args.camera}.\n\n"
              "Another app may be using the webcam, or the index is wrong. "
              "Close other apps using the camera, or try a different index with "
              "run-clean.bat --camera 1 (then 2, etc.).")

    ok, probe = cap.read()
    if not ok:
        fatal("The camera opened but returned no frame. Close other apps that "
              "may be using the webcam and try again.")
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
        cap.release()
        fatal("--no-preview was set but the virtual camera is not available, so "
              "there would be no output. Remove --no-preview, or set up the OBS "
              "virtual camera (run diagnose.bat for help).")

    WINDOW = "face-pixelate-cam (preview)"
    # Mutable UI state shared with the mouse callback. Overlay starts HIDDEN so
    # the preview is clean; only the small corner button shows.
    ui = {"show_overlay": False, "show_picker": False,
          "picker_geom": None, "wheel_drag": False}

    def on_mouse(event, mx, my, flags, param):
        # Accent picker gets first dibs on the mouse when it is open.
        if ui["show_picker"] and ui["picker_geom"] is not None:
            if event == cv2.EVENT_LBUTTONDOWN:
                if picker_click(s, ui["picker_geom"], mx, my, dragging=False):
                    ui["wheel_drag"] = True
                    return
            elif event == cv2.EVENT_MOUSEMOVE and ui["wheel_drag"]:
                picker_click(s, ui["picker_geom"], mx, my, dragging=True)
                return
            elif event == cv2.EVENT_LBUTTONUP:
                ui["wheel_drag"] = False
        if event == cv2.EVENT_LBUTTONDOWN and point_in_button(mx, my):
            ui["show_overlay"] = not ui["show_overlay"]

    if not args.no_preview:
        cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(WINDOW, on_mouse)

    # Kick off the background update check (silent on failure/offline).
    update_state = {"update": None, "dismissed": False, "downloading": False,
                    "status": None, "first_seen": None}
    if not args.no_update_check:
        start_update_check(update_state)

    t_prev = time.time()
    fps = 0.0

    print(f"Running face-pixelate-cam v{APP_VERSION}. "
          "Close the window (X) or press 'q' to quit.")
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
                # Rebuild the theme each frame so accent/theme changes are live.
                theme = build_theme(s["theme"], s["accent"])
                disp = frame.copy()
                # Clean mode starts bare (no button, no overlay) so the captured
                # window is uncluttered. Pressing 'h' -- or clicking the top-left
                # corner -- still summons the full controls when you need them;
                # press 'h' again to go bare for streaming.
                if ui["show_overlay"]:
                    draw_help(disp, s, fps, vcam is not None, theme)
                    draw_button(disp, ui["show_overlay"], theme)
                elif not args.clean:
                    draw_button(disp, ui["show_overlay"], theme)

                # Accent picker panel (toggle with 't').
                if ui["show_picker"]:
                    geom = picker_geometry(disp.shape[1], disp.shape[0])
                    ui["picker_geom"] = geom
                    draw_picker(disp, s, theme, geom)
                else:
                    ui["picker_geom"] = None

                # Update banner (drawn on the preview copy only -> never hits the
                # virtual camera). Auto-hides after UPDATE_BANNER_SECONDS so it
                # cannot linger on a Window-Capture stream.
                upd = update_state["update"]
                show_banner = False
                if upd and not update_state["dismissed"]:
                    if update_state["first_seen"] is None:
                        update_state["first_seen"] = time.time()
                    elapsed = time.time() - update_state["first_seen"]
                    if update_state["status"]:
                        banner = update_state["status"]
                        show_banner = elapsed < (UPDATE_BANNER_SECONDS + 15)
                    else:
                        banner = (f"Update {upd.get('tag','')} available - "
                                  "press U to download, N to dismiss")
                        show_banner = elapsed < UPDATE_BANNER_SECONDS
                    if show_banner:
                        draw_update_banner(disp, banner, theme)

                cv2.imshow(WINDOW, disp)
                key = cv2.waitKey(1) & 0xFF

                # Quit if the window was closed via its X button.
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
                if key in (ord('q'), 27):
                    break
                elif key in (ord('u'), ord('U')):
                    if upd and not update_state["dismissed"]:
                        start_download(upd, update_state)
                elif key in (ord('n'), ord('N')):
                    update_state["dismissed"] = True
                elif key in (ord('t'), ord('T')):
                    ui["show_picker"] = not ui["show_picker"]
                    if not ui["show_picker"]:
                        save_settings(s)  # persist accent/theme on picker close
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
