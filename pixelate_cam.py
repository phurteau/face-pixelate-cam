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

The app opens a single window that shows your pixelated video. A hamburger
button in the top-left corner opens a control panel with labeled sliders,
toggles and an accent color picker, so nothing has to be memorized. The
hamburger auto-hides when the mouse is idle, so capturing this window with a
'Window Capture' source in OBS/Streamlabs stays clean.

Run:  python pixelate_cam.py         (opens the app window)
      python pixelate_cam.py --help  (all options)

A few keyboard shortcuts still work when the window has focus:
  q / ESC : quit
  h       : open / close the control panel
  p       : toggle pixelation on/off (panic peek)
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
ICON_PATH = os.path.join(BUNDLE_DIR, "app.ico")


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


def notify(title, msg):
    """Non-fatal notification that works even with no console (pythonw launch).

    Writes the message to run-log.txt and shows a non-blocking Windows message
    box (on a background thread so it never freezes the video).
    """
    print(msg)
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    if os.name == "nt":
        def _box():
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, msg, title, 0x40)
            except Exception:
                pass
        threading.Thread(target=_box, daemon=True).start()


# ---------------------------------------------------------------------------
# Auto-update: check GitHub Releases for a newer version and offer a one-click
# download. Runs in a background thread; failures (offline, rate-limited) are
# silently ignored so they never disrupt streaming.
# ---------------------------------------------------------------------------

APP_VERSION = "1.5.0"
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
    "mirror": False,       # selfie/mirror view
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
# HSV helpers + color wheel (used by the accent picker)
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
    """Return (rgb_image, circle_mask) for an HSV wheel at the given Value.

    Cached by (radius, quantized value). hue = angle, saturation = radius.
    Returns an RGB image (for PIL/Tkinter) plus the boolean circle mask.
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
    rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    rgb[~mask] = 0
    _WHEEL_CACHE[key] = (rgb, mask)
    if len(_WHEEL_CACHE) > 24:
        _WHEEL_CACHE.pop(next(iter(_WHEEL_CACHE)))
    return rgb, mask


# Full help text shown when the virtual camera cannot start.
VCAM_HELP = (
    "face-pixelate-cam could NOT start the virtual camera, so it will not "
    "appear as a camera in Teams / OBS / Streamlabs.\n\n"
    "Reason: %s\n\n"
    "This app publishes through the OBS Studio virtual camera, which must be "
    "installed and registered first:\n"
    "  1. Install OBS Studio  (https://obsproject.com)\n"
    "  2. Open OBS once, click 'Start Virtual Camera' then 'Stop Virtual "
    "Camera'\n"
    "  3. Close OBS and try again\n\n"
    "Then pick 'OBS Virtual Camera' in Teams/OBS/Streamlabs and keep this app "
    "running. (Streamlabs' own virtual camera is a different device and will "
    "NOT work here.)\n\n"
    "No virtual camera needed? Just capture this window with a 'Window "
    "Capture' source instead. Run diagnose.bat for a full check."
)


# ---------------------------------------------------------------------------
# Video engine: camera capture + processing + virtual camera on a worker thread.
# The Tkinter UI never blocks on camera IO; it only reads the latest frame.
# ---------------------------------------------------------------------------

class VideoEngine:
    def __init__(self, args, s):
        self.args = args
        self.s = s
        # Fails fast with a clear message if the YuNet model is missing.
        self.tracker = FaceTracker(MODEL_PATH, s["min_confidence"], s["hold_frames"])

        self._lock = threading.Lock()
        self._frame = None            # latest processed BGR frame (read-only once stored)
        self.fps = 0.0
        self.frame_w = args.width
        self.frame_h = args.height

        self._cam_index = None
        self._req_cam = int(args.camera)
        self._req_res = (int(args.width), int(args.height))
        self._applied_res = None
        self.cam_error = None

        self._want_vcam = False       # off by default; started from the panel
        self._vcam = None
        self.vcam_active = False
        self.vcam_status = "off"      # "off" | "on" | "error"
        self.vcam_error = None
        self._notified_vcam_error = False

        self._gamma_val = s["gamma"]
        self._gamma_lut = build_gamma_lut(self._gamma_val)

        self._stop = threading.Event()
        self._thread = None

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)

    # -- requests from the UI thread ---------------------------------------
    def request_camera(self, index):
        self._req_cam = int(index)

    def request_resolution(self, w, h):
        self._req_res = (int(w), int(h))

    def set_vcam_enabled(self, on):
        if on and not HAVE_VCAM:
            self.vcam_status = "error"
            self.vcam_error = "pyvirtualcam is not installed"
            return
        self._want_vcam = bool(on)
        if on:
            self._notified_vcam_error = False   # allow a fresh attempt + popup

    def get_frame(self):
        with self._lock:
            return self._frame

    # -- worker -------------------------------------------------------------
    def _open_camera(self, index, w, h):
        if self.args.test_pattern:
            self._cam_index = index
            self._applied_res = (w, h)
            return None
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if os.name == "nt" else 0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, self.args.fps)
        self._cam_index = index
        self._applied_res = (w, h)
        return cap

    def _synthetic_frame(self):
        w, h = self._req_res
        t = time.time()
        frame = np.zeros((h, w, 3), np.uint8)
        frame[:, :, 0] = np.linspace(40, 150, w).astype(np.uint8)
        frame[:, :, 2] = np.tile(np.linspace(60, 170, h), (w, 1)).T.astype(np.uint8)
        cx = int(w / 2 + np.sin(t) * w * 0.15)
        cy = int(h / 2 + np.cos(t * 0.7) * h * 0.1)
        cv2.ellipse(frame, (cx, cy), (110, 150), 0, 0, 360, (150, 170, 200), -1)
        cv2.circle(frame, (cx - 40, cy - 45), 16, (60, 60, 60), -1)
        cv2.circle(frame, (cx + 40, cy - 45), 16, (60, 60, 60), -1)
        time.sleep(1.0 / max(1, self.args.fps))
        return frame

    def _run(self):
        cap = self._open_camera(self._req_cam, *self._req_res)
        if cap is not None and not cap.isOpened():
            self.cam_error = f"Could not open camera index {self._req_cam}."
        t_prev = time.time()

        while not self._stop.is_set():
            # Apply pending camera / resolution changes.
            if self._req_cam != self._cam_index or self._req_res != self._applied_res:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                self._close_vcam()      # size may change; recreate on demand
                cap = self._open_camera(self._req_cam, *self._req_res)
                if cap is not None and not cap.isOpened():
                    self.cam_error = f"Could not open camera index {self._req_cam}."
                    time.sleep(0.2)
                    continue
                self.cam_error = None

            # Grab a frame (or synthesize one in test mode).
            if self.args.test_pattern:
                frame = self._synthetic_frame()
                ok = True
            else:
                ok, frame = cap.read()
            if not ok or frame is None:
                if not self.cam_error:
                    self.cam_error = "Camera opened but returned no frame."
                time.sleep(0.03)
                continue
            self.cam_error = None

            if self.s.get("mirror"):
                frame = cv2.flip(frame, 1)

            # 1) Lighting (rebuild the gamma LUT only when gamma changes).
            if self._gamma_val != self.s["gamma"]:
                self._gamma_val = self.s["gamma"]
                self._gamma_lut = build_gamma_lut(self._gamma_val)
            frame = apply_lighting(frame, self.s, self._gamma_lut)

            # 2) Face pixelation (faces only).
            if self.s["pixelate_on"]:
                for (x0, y0, x1, y1) in self.tracker.detect(frame, self.s["padding"]):
                    pixelate_region(frame, x0, y0, x1, y1, self.s["block"])

            h, w = frame.shape[:2]
            self.frame_w, self.frame_h = w, h

            # 3) Virtual camera (create/destroy on demand, then send).
            self._service_vcam(frame, w, h)

            # FPS meter.
            now = time.time()
            dt = now - t_prev
            t_prev = now
            if dt > 0:
                self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

            with self._lock:
                self._frame = frame

            if self._vcam is None and not self.args.test_pattern:
                time.sleep(0.001)   # yield; keep CPU sane without vcam pacing

        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        self._close_vcam()

    def _service_vcam(self, frame, w, h):
        if self._want_vcam and self._vcam is None and HAVE_VCAM:
            try:
                self._vcam = pyvirtualcam.Camera(width=w, height=h,
                                                 fps=self.args.fps,
                                                 fmt=PixelFormat.BGR)
                self.vcam_active = True
                self.vcam_status = "on"
                self.vcam_error = None
            except Exception as e:
                self._vcam = None
                self.vcam_active = False
                self.vcam_status = "error"
                self.vcam_error = (str(e).splitlines() or ["unknown error"])[0]
                self._want_vcam = False   # stop retrying until the user asks again
                if not self._notified_vcam_error:
                    self._notified_vcam_error = True
                    notify("face-pixelate-cam - virtual camera unavailable",
                           VCAM_HELP % self.vcam_error)
        if self._vcam is not None:
            try:
                self._vcam.send(frame)
                self._vcam.sleep_until_next_frame()
            except Exception as e:
                self.vcam_error = (str(e).splitlines() or ["send failed"])[0]
                self._close_vcam()
                self.vcam_status = "error"
        elif not self._want_vcam:
            self._close_vcam()

    def _close_vcam(self):
        if self._vcam is not None:
            try:
                self._vcam.close()
            except Exception:
                pass
        self._vcam = None
        self.vcam_active = False
        if self.vcam_status == "on":
            self.vcam_status = "off"


# ---------------------------------------------------------------------------
# Tkinter GUI: video fills the window; a corner hamburger opens the controls.
# ---------------------------------------------------------------------------

DRAWER_W = 340
RES_CHOICES = ["640x360", "1280x720", "1920x1080"]
CAM_CHOICES = ["0", "1", "2", "3"]


def _tk_palette(theme_name, accent_hex):
    """Resolve theme tokens to Tk-friendly hex strings (one accent drives all)."""
    base = THEME_TOKENS.get(theme_name, THEME_TOKENS["dark"])
    der = derive_accent(accent_hex)
    r, g, b = hex_to_rgb(accent_hex)
    yiq = (r * 299 + g * 587 + b * 114) / 1000.0
    ink = "#08140a" if yiq > 140 else "#ffffff"
    return {
        "bg": base["bg"], "bg2": base["bg2"], "panel": base["panel"],
        "panel2": base["panel2"], "line": base["line"], "txt": base["txt"],
        "dim": base["dim"], "acc": der["acc_hex"], "acc2": der["acc2_hex"],
        "ink": ink,
    }


class App:
    def __init__(self, engine, s, args):
        import tkinter as tk
        self.tk = tk
        self.engine = engine
        self.s = s
        self.args = args

        self.pal = _tk_palette(s["theme"], s["accent"])
        # Widgets tagged by role so a theme/accent change can restyle everything.
        self._roles = []          # list of (widget, role)
        self._menus = []          # OptionMenu popup menus to recolor
        self._scales = {}         # key -> Scale
        self._photo = None        # keep a ref so Tk does not GC the frame image
        self.drawer_open = False
        self._hamburger_visible = True
        self._last_motion = time.time()

        self.update_state = {"update": None, "dismissed": False,
                             "downloading": False, "status": None}

        root = tk.Tk()
        self.root = root
        root.title("face-pixelate-cam")
        root.geometry("1180x680")
        root.minsize(760, 460)
        root.configure(bg=self.pal["bg"])
        try:
            if os.name == "nt" and os.path.exists(ICON_PATH):
                root.iconbitmap(ICON_PATH)
        except Exception:
            pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Video surface fills the window; controls float on top via place().
        self.video = tk.Canvas(root, bg=self.pal["bg"], highlightthickness=0,
                               bd=0)
        self.video.pack(fill="both", expand=True)
        self._img_item = self.video.create_image(0, 0, anchor="nw")
        self._msg_item = self.video.create_text(
            0, 0, text="", fill=self.pal["dim"], font=("Segoe UI", 15),
            anchor="c")

        self._build_hamburger()
        self._build_drawer()

        root.bind("<Motion>", self._on_motion)
        root.bind("<Key>", self._on_key)

        self._apply_palette()
        if not args.no_update_check:
            start_update_check(self.update_state)

        self.root.after(30, self._render_loop)
        self.root.after(1000, self._idle_check)

    # -- construction -------------------------------------------------------
    def _reg(self, widget, role):
        self._roles.append((widget, role))
        return widget

    def _build_hamburger(self):
        tk = self.tk
        c = tk.Canvas(self.video, width=42, height=34, highlightthickness=1,
                      bd=0, cursor="hand2")
        self.ham = c
        c.place(x=12, y=12)
        c.bind("<Button-1>", lambda e: self._toggle_drawer())
        self._draw_ham_icon()

    def _draw_ham_icon(self):
        c = self.ham
        c.delete("all")
        c.configure(bg=self.pal["panel"], highlightbackground=self.pal["line"])
        col = self.pal["txt"]
        for i in range(3):
            yy = 11 + i * 6
            c.create_line(12, yy, 30, yy, fill=col, width=2)

    def _build_drawer(self):
        tk = self.tk
        # Docked control panel: a solid frame placed inside the main window
        # (hidden until the hamburger is clicked). Simple and reliable.
        self.drawer = tk.Frame(self.root, bg=self.pal["panel"], bd=0,
                               highlightthickness=1)
        # Scrollable inner area.
        self.dcanvas = tk.Canvas(self.drawer, bg=self.pal["panel"],
                                 highlightthickness=0, bd=0, width=DRAWER_W - 16)
        self.dscroll = tk.Scrollbar(self.drawer, orient="vertical",
                                    command=self.dcanvas.yview)
        self.dcanvas.configure(yscrollcommand=self.dscroll.set)
        self.dscroll.pack(side="right", fill="y")
        self.dcanvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.dcanvas, bg=self.pal["panel"])
        self._inner_win = self.dcanvas.create_window((0, 0), window=self.inner,
                                                     anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.dcanvas.configure(
            scrollregion=self.dcanvas.bbox("all")))
        self.dcanvas.bind("<Configure>", lambda e: self.dcanvas.itemconfig(
            self._inner_win, width=e.width))
        self.dcanvas.bind("<Enter>", lambda e: self.dcanvas.bind_all(
            "<MouseWheel>", self._on_wheel_scroll))
        self.dcanvas.bind("<Leave>", lambda e: self.dcanvas.unbind_all(
            "<MouseWheel>"))

        self._build_controls(self.inner)

    def _on_wheel_scroll(self, e):
        self.dcanvas.yview_scroll(int(-e.delta / 120), "units")

    def _header(self, parent, text):
        tk = self.tk
        f = self._reg(tk.Frame(parent, bg=self.pal["panel"]), "panel")
        f.pack(fill="x", padx=14, pady=(14, 2))
        lbl = self._reg(tk.Label(f, text=text, bg=self.pal["panel"],
                                 fg=self.pal["dim"], anchor="w",
                                 font=("Segoe UI Semibold", 10)), "dim")
        lbl.pack(fill="x")
        sep = self._reg(tk.Frame(f, bg=self.pal["line"], height=1), "line")
        sep.pack(fill="x", pady=(3, 0))
        return f

    def _row(self, parent):
        tk = self.tk
        f = self._reg(tk.Frame(parent, bg=self.pal["panel"]), "panel")
        f.pack(fill="x", padx=14, pady=3)
        return f

    def _label(self, parent, text, role="txt", **kw):
        tk = self.tk
        fg = self.pal[role] if role in self.pal else self.pal["txt"]
        lbl = tk.Label(parent, text=text, bg=self.pal["panel"], fg=fg,
                       anchor="w", font=("Segoe UI", 10), **kw)
        return self._reg(lbl, role)

    def _add_slider(self, key, label, frm, to, res, is_int):
        tk = self.tk
        row = self._row(self.inner)
        top = self._reg(tk.Frame(row, bg=self.pal["panel"]), "panel")
        top.pack(fill="x")
        self._label(top, label).pack(side="left")
        sc = tk.Scale(row, from_=frm, to=to, resolution=res,
                      orient="horizontal", showvalue=True,
                      bg=self.pal["panel"], fg=self.pal["txt"],
                      troughcolor=self.pal["panel2"],
                      activebackground=self.pal["acc2"],
                      highlightthickness=0, bd=0, sliderrelief="flat",
                      font=("Segoe UI", 8),
                      command=lambda v, k=key, i=is_int: self._on_slider(k, v, i))
        sc.set(self.s[key])
        sc.pack(fill="x")
        self._reg(sc, "scale")
        self._scales[key] = sc

    def _on_slider(self, key, value, is_int):
        v = int(float(value)) if is_int else round(float(value), 2)
        self.s[key] = v

    def _button(self, parent, text, cmd, primary=False):
        tk = self.tk
        role = "primary" if primary else "button"
        b = tk.Button(parent, text=text, command=cmd, relief="flat", bd=0,
                      padx=10, pady=5, cursor="hand2",
                      font=("Segoe UI", 10),
                      activeforeground=self.pal["ink"] if primary else self.pal["txt"])
        return self._reg(b, role)

    def _build_controls(self, parent):
        tk = self.tk

        # Header with a Close button. The corner hamburger opens the drawer;
        # this button closes it again.
        head = self._row(parent)
        title = self._label(head, "Controls")
        title.configure(font=("Segoe UI Semibold", 13))
        title.pack(side="left")
        self._button(head, "Close", self._toggle_drawer).pack(side="right")

        # ---- Output ----
        self._header(parent, "OUTPUT")
        row = self._row(parent)
        self._label(row, "Camera").pack(side="left")
        self.var_cam = tk.StringVar(value=str(self.args.camera))
        om = tk.OptionMenu(row, self.var_cam, *CAM_CHOICES,
                           command=lambda v: self.engine.request_camera(int(v)))
        om.configure(relief="flat", bd=0, highlightthickness=0,
                     font=("Segoe UI", 10), cursor="hand2", width=4)
        om.pack(side="right")
        self._reg(om, "option")
        self._menus.append(om["menu"])

        row = self._row(parent)
        self._label(row, "Resolution").pack(side="left")
        self.var_res = tk.StringVar(value=f"{self.args.width}x{self.args.height}")
        om = tk.OptionMenu(row, self.var_res, *RES_CHOICES,
                           command=self._on_res)
        om.configure(relief="flat", bd=0, highlightthickness=0,
                     font=("Segoe UI", 10), cursor="hand2", width=10)
        om.pack(side="right")
        self._reg(om, "option")
        self._menus.append(om["menu"])

        self.var_mirror = tk.BooleanVar(value=bool(self.s.get("mirror")))
        cb = tk.Checkbutton(self._row(parent), text="Mirror (selfie view)",
                            variable=self.var_mirror, command=self._on_mirror,
                            anchor="w", font=("Segoe UI", 10), bd=0,
                            highlightthickness=0, cursor="hand2")
        cb.pack(fill="x")
        self._reg(cb, "check")

        row = self._row(parent)
        self.btn_vcam = self._button(row, "Start Virtual Camera",
                                     self._toggle_vcam, primary=True)
        self.btn_vcam.pack(side="left")
        self.lbl_vcam = self._label(row, "off", role="dim")
        self.lbl_vcam.configure(padx=10)
        self.lbl_vcam.pack(side="left")

        # ---- Pixelation ----
        self._header(parent, "PIXELATION")
        self.var_pix = tk.BooleanVar(value=bool(self.s["pixelate_on"]))
        cb = tk.Checkbutton(self._row(parent), text="Pixelate faces",
                            variable=self.var_pix, command=self._on_pix,
                            anchor="w", font=("Segoe UI", 10), bd=0,
                            highlightthickness=0, cursor="hand2")
        cb.pack(fill="x")
        self._reg(cb, "check")
        self._add_slider("block", "Block size", 6, 60, 2, True)
        self._add_slider("padding", "Face padding (safety margin)", 0.0, 1.0,
                         0.05, False)

        # ---- Lighting ----
        self._header(parent, "LIGHTING")
        self._add_slider("brightness", "Brightness", -100, 100, 5, False)
        self._add_slider("contrast", "Contrast", 0.5, 2.0, 0.05, False)
        self._add_slider("saturation", "Saturation", 0.0, 2.0, 0.05, False)
        self._add_slider("warmth", "Warmth", -50, 50, 1, False)
        self._add_slider("gamma", "Gamma", 0.4, 2.5, 0.05, False)
        row = self._row(parent)
        self._button(row, "Reset lighting", self._reset_lighting).pack(side="left")

        # ---- Appearance ----
        self._header(parent, "APPEARANCE")
        row = self._row(parent)
        self.btn_theme = self._button(row, f"Theme: {self.s['theme']}",
                                      self._toggle_theme)
        self.btn_theme.pack(side="left")
        self._build_accent(parent)

        # ---- Footer ----
        self._header(parent, "")
        row = self._row(parent)
        self._button(row, "Save settings", self._save, primary=True).pack(side="left")
        self._button(row, "Reset all", self._reset_all).pack(side="left", padx=(8, 0))

        self.lbl_update = self._label(self._row(parent), "", role="acc")
        self.lbl_update.pack_forget()
        self.btn_update = self._button(self._row(parent), "Download update",
                                       self._download_update, primary=True)
        self.btn_update.master.pack_forget()

        ver = self._label(self._row(parent),
                          f"face-pixelate-cam v{APP_VERSION}", role="dim")
        ver.pack(side="left", pady=(6, 14))

    def _build_accent(self, parent):
        tk = self.tk
        self.wheel_R = 74
        row = self._row(parent)
        wrap = self._reg(tk.Frame(row, bg=self.pal["panel"]), "panel")
        wrap.pack(fill="x")
        self.wheel = tk.Canvas(wrap, width=self.wheel_R * 2,
                               height=self.wheel_R * 2, highlightthickness=0,
                               bd=0, bg=self.pal["panel"], cursor="crosshair")
        self.wheel.pack(side="left")
        self.wheel.bind("<Button-1>", self._on_wheel_pick)
        self.wheel.bind("<B1-Motion>", self._on_wheel_pick)
        self._wheel_img_item = self.wheel.create_image(0, 0, anchor="nw")
        self._wheel_dot = self.wheel.create_oval(0, 0, 0, 0, outline="#000",
                                                 width=1, fill="#fff")

        right = self._reg(tk.Frame(wrap, bg=self.pal["panel"]), "panel")
        right.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self._label(right, "Brightness", role="dim").pack(anchor="w")
        self.var_val = tk.DoubleVar(value=self._accent_value() * 100)
        self.sc_val = tk.Scale(right, from_=0, to=100, resolution=1,
                               orient="horizontal", showvalue=False,
                               variable=self.var_val, command=self._on_value,
                               bg=self.pal["panel"], fg=self.pal["txt"],
                               troughcolor=self.pal["panel2"],
                               activebackground=self.pal["acc2"],
                               highlightthickness=0, bd=0, sliderrelief="flat")
        self.sc_val.pack(fill="x")
        self._reg(self.sc_val, "scale")

        srow = self._reg(tk.Frame(right, bg=self.pal["panel"]), "panel")
        srow.pack(fill="x", pady=(6, 0))
        self.swatch = tk.Canvas(srow, width=26, height=20, highlightthickness=1,
                                bd=0)
        self.swatch.pack(side="left")
        self._reg(self.swatch, "swatch")
        self.var_hex = tk.StringVar(value=self.s["accent"].upper())
        self.ent_hex = tk.Entry(srow, textvariable=self.var_hex, width=9,
                                relief="flat", bd=2, font=("Consolas", 10))
        self.ent_hex.pack(side="left", padx=(8, 0))
        self.ent_hex.bind("<Return>", self._on_hex)
        self._reg(self.ent_hex, "entry")
        self._button(srow, "Reset", self._reset_accent).pack(side="left",
                                                             padx=(8, 0))
        self._render_wheel()
        self._update_accent_ui()

    # -- accent picker helpers ---------------------------------------------
    def _accent_value(self):
        r, g, b = hex_to_rgb(self.s["accent"])
        _, _, v = rgb_to_hsv(r, g, b)
        return v

    def _render_wheel(self):
        from PIL import Image, ImageTk
        R = self.wheel_R
        val = max(0.02, self.var_val.get() / 100.0)
        rgb, mask = render_color_wheel(R, val)
        rgba = np.dstack([rgb, (mask * 255).astype(np.uint8)])
        img = Image.fromarray(rgba, "RGBA")
        self._wheel_photo = ImageTk.PhotoImage(img)
        self.wheel.itemconfig(self._wheel_img_item, image=self._wheel_photo)

    def _place_dot(self):
        R = self.wheel_R
        r, g, b = hex_to_rgb(self.s["accent"])
        h, sat, _ = rgb_to_hsv(r, g, b)
        dx = np.cos(np.radians(h)) * sat * R
        dy = -np.sin(np.radians(h)) * sat * R
        x, y = R + dx, R + dy
        self.wheel.coords(self._wheel_dot, x - 6, y - 6, x + 6, y + 6)

    def _on_wheel_pick(self, e):
        R = self.wheel_R
        dx, dy = e.x - R, e.y - R
        dist = (dx * dx + dy * dy) ** 0.5
        h = np.degrees(np.arctan2(-dy, dx)) % 360.0
        sat = min(1.0, dist / R)
        val = max(0.02, self.var_val.get() / 100.0)
        col = hsv_to_rgb(h, sat, val)
        self._set_accent(rgb_to_hex(col))

    def _on_value(self, _v):
        # Keep hue/sat, change Value.
        r, g, b = hex_to_rgb(self.s["accent"])
        h, sat, _ = rgb_to_hsv(r, g, b)
        val = max(0.02, self.var_val.get() / 100.0)
        col = hsv_to_rgb(h, sat, val)
        self.s["accent"] = rgb_to_hex(col)
        self.pal = _tk_palette(self.s["theme"], self.s["accent"])
        self._render_wheel()
        self._update_accent_ui()
        self._apply_palette()

    def _on_hex(self, _e):
        txt = self.var_hex.get().strip()
        if not txt.startswith("#"):
            txt = "#" + txt
        if re.fullmatch(r"#[0-9a-fA-F]{6}", txt):
            self._set_accent(txt.lower())

    def _set_accent(self, hex_str):
        self.s["accent"] = hex_str
        self.pal = _tk_palette(self.s["theme"], self.s["accent"])
        self.var_val.set(self._accent_value() * 100)
        self._render_wheel()
        self._update_accent_ui()
        self._apply_palette()

    def _reset_accent(self):
        self._set_accent(DEFAULT_ACCENT)

    def _update_accent_ui(self):
        self.var_hex.set(self.s["accent"].upper())
        try:
            self.swatch.configure(bg=self.s["accent"])
        except Exception:
            pass
        self._place_dot()

    # -- control callbacks --------------------------------------------------
    def _on_res(self, value):
        try:
            w, h = (int(x) for x in value.lower().split("x"))
            self.engine.request_resolution(w, h)
        except Exception:
            pass

    def _on_mirror(self):
        self.s["mirror"] = bool(self.var_mirror.get())

    def _on_pix(self):
        self.s["pixelate_on"] = bool(self.var_pix.get())

    def _toggle_vcam(self):
        turning_on = not self.engine.vcam_active and self.engine.vcam_status != "on"
        self.engine.set_vcam_enabled(turning_on)

    def _reset_lighting(self):
        for k in ("brightness", "contrast", "saturation", "warmth", "gamma"):
            self.s[k] = DEFAULTS[k]
            if k in self._scales:
                self._scales[k].set(self.s[k])

    def _toggle_theme(self):
        self.s["theme"] = "light" if self.s["theme"] == "dark" else "dark"
        self.pal = _tk_palette(self.s["theme"], self.s["accent"])
        self.btn_theme.configure(text=f"Theme: {self.s['theme']}")
        self._apply_palette()

    def _save(self):
        save_settings(self.s)

    def _reset_all(self):
        for k, v in DEFAULTS.items():
            self.s[k] = v
            if k in self._scales:
                self._scales[k].set(v)
        self.var_pix.set(self.s["pixelate_on"])
        self.var_mirror.set(self.s.get("mirror", False))
        self.pal = _tk_palette(self.s["theme"], self.s["accent"])
        self.btn_theme.configure(text=f"Theme: {self.s['theme']}")
        self.var_val.set(self._accent_value() * 100)
        self._render_wheel()
        self._update_accent_ui()
        self._apply_palette()

    def _download_update(self):
        upd = self.update_state.get("update")
        if upd:
            start_download(upd, self.update_state)

    # -- drawer + hamburger -------------------------------------------------
    def _toggle_drawer(self):
        self.drawer_open = not self.drawer_open
        if self.drawer_open:
            self.drawer.place(x=0, y=0, relheight=1.0, width=DRAWER_W)
            self.drawer.lift()
            self.ham.place_forget()
            self._hamburger_visible = False
        else:
            self.drawer.place_forget()
            self.ham.place(x=12, y=12)
            self._hamburger_visible = True
            self._last_motion = time.time()

    def _on_motion(self, _e):
        self._last_motion = time.time()
        if self.drawer_open:
            return
        if not self._hamburger_visible:
            self.ham.place(x=12, y=12)
            self._hamburger_visible = True

    def _idle_check(self):
        # Hide the hamburger after a few idle seconds when the drawer is closed,
        # so a Window Capture of this window stays perfectly clean.
        if (not self.drawer_open and self._hamburger_visible
                and time.time() - self._last_motion > 3.0):
            self.ham.place_forget()
            self._hamburger_visible = False
        self.root.after(1000, self._idle_check)

    def _on_key(self, e):
        k = (e.keysym or "").lower()
        if k in ("q", "escape"):
            self.on_close()
        elif k == "h":
            self._toggle_drawer()
        elif k == "p":
            self.s["pixelate_on"] = not self.s["pixelate_on"]
            self.var_pix.set(self.s["pixelate_on"])

    # -- theming ------------------------------------------------------------
    def _apply_palette(self):
        p = self.pal
        self.root.configure(bg=p["bg"])
        self.video.configure(bg=p["bg"])
        self.video.itemconfig(self._msg_item, fill=p["dim"])
        self.drawer.configure(bg=p["line"])
        self.dcanvas.configure(bg=p["panel"])
        self.inner.configure(bg=p["panel"])
        for w, role in self._roles:
            try:
                self._style(w, role)
            except Exception:
                pass
        for menu in self._menus:
            try:
                menu.configure(bg=p["panel2"], fg=p["txt"],
                               activebackground=p["acc"],
                               activeforeground=p["ink"], bd=0)
            except Exception:
                pass
        self._draw_ham_icon()
        self._update_accent_ui()

    def _style(self, w, role):
        p = self.pal
        if role in ("panel",):
            w.configure(bg=p["panel"])
        elif role == "line":
            w.configure(bg=p["line"])
        elif role in ("txt", "dim", "acc"):
            w.configure(bg=p["panel"], fg=p[role if role != "acc" else "acc"])
        elif role == "scale":
            w.configure(bg=p["panel"], fg=p["txt"], troughcolor=p["panel2"],
                        activebackground=p["acc2"], highlightthickness=0)
        elif role == "button":
            w.configure(bg=p["panel2"], fg=p["txt"],
                        activebackground=p["acc2"], activeforeground=p["txt"])
        elif role == "primary":
            w.configure(bg=p["acc"], fg=p["ink"],
                        activebackground=p["acc2"], activeforeground=p["ink"])
        elif role == "check":
            w.configure(bg=p["panel"], fg=p["txt"], selectcolor=p["panel2"],
                        activebackground=p["panel"], activeforeground=p["txt"])
        elif role == "option":
            w.configure(bg=p["panel2"], fg=p["txt"],
                        activebackground=p["acc"], highlightthickness=0)
        elif role == "entry":
            w.configure(bg=p["panel2"], fg=p["txt"], insertbackground=p["txt"],
                        highlightthickness=1, highlightbackground=p["line"])
        elif role == "swatch":
            w.configure(bg=self.s["accent"], highlightbackground=p["line"])

    # -- render loop --------------------------------------------------------
    def _render_loop(self):
        try:
            self._draw_frame()
            self._refresh_status()
        except Exception:
            pass
        self.root.after(30, self._render_loop)

    def _draw_frame(self):
        from PIL import Image, ImageTk
        frame = self.engine.get_frame()
        cw = max(1, self.video.winfo_width())
        ch = max(1, self.video.winfo_height())
        if frame is None:
            msg = self.engine.cam_error or "Starting camera..."
            self.video.itemconfig(self._msg_item, text=msg)
            self.video.coords(self._msg_item, cw // 2, ch // 2)
            return
        self.video.itemconfig(self._msg_item, text="")
        fh, fw = frame.shape[:2]
        x0 = DRAWER_W if self.drawer_open else 0
        region_w = max(1, cw - x0)
        scale = min(region_w / fw, ch / fh)
        dw, dh = max(1, int(fw * scale)), max(1, int(fh * scale))
        ox = x0 + (region_w - dw) // 2
        oy = (ch - dh) // 2
        resized = cv2.resize(frame, (dw, dh))
        img = Image.fromarray(resized[:, :, ::-1])
        self._photo = ImageTk.PhotoImage(img)
        self.video.coords(self._img_item, ox, oy)
        self.video.itemconfig(self._img_item, image=self._photo)

    def _refresh_status(self):
        eng = self.engine
        st = eng.vcam_status
        if st == "on":
            self.lbl_vcam.configure(text="On", fg=self.pal["acc2"])
            self.btn_vcam.configure(text="Stop Virtual Camera")
        elif st == "error":
            short = (eng.vcam_error or "error")[:26]
            self.lbl_vcam.configure(text=f"Error: {short}", fg="#e06060")
            self.btn_vcam.configure(text="Start Virtual Camera")
        else:
            self.lbl_vcam.configure(text="Off", fg=self.pal["dim"])
            self.btn_vcam.configure(text="Start Virtual Camera")

        upd = self.update_state.get("update")
        if upd and not self.update_state.get("dismissed"):
            status = self.update_state.get("status")
            text = status or f"Update {upd.get('tag','')} available"
            self.lbl_update.configure(text=text)
            if not self.lbl_update.winfo_ismapped():
                self.lbl_update.pack(anchor="w", padx=14)
                self.btn_update.master.pack(fill="x", padx=14, pady=2)

    # -- lifecycle ----------------------------------------------------------
    def on_close(self):
        try:
            save_settings(self.s)
        except Exception:
            pass
        try:
            self.engine.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Pixelate faces in your webcam feed.")
    ap.add_argument("--camera", type=int, default=0, help="Webcam index (default 0)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--no-vcam", action="store_true",
                    help="Do not start the virtual camera automatically. You can "
                         "still start it from the controls, or just capture this "
                         "window with a 'Window Capture' source.")
    ap.add_argument("--mirror", action="store_true",
                    help="Mirror the image (selfie view).")
    ap.add_argument("--accent", default=None,
                    help="Set the accent color as #rrggbb (persists to settings).")
    ap.add_argument("--theme", choices=["dark", "light"], default=None,
                    help="Set the UI theme (persists to settings).")
    ap.add_argument("--no-update-check", action="store_true",
                    help="Do not check GitHub for a newer version on launch.")
    # Accepted for backward compatibility with older shortcuts; the GUI always
    # shows the video, and the settings live behind the corner menu.
    ap.add_argument("--clean", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-preview", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--test-pattern", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version",
                    version=f"face-pixelate-cam {APP_VERSION}")
    args = ap.parse_args()

    if not HAVE_PIL:
        fatal("Pillow is required for the app window. Run setup.bat to install "
              "dependencies, then try again.")

    s = load_settings()
    if args.accent:
        s["accent"] = args.accent
    if args.theme:
        s["theme"] = args.theme
    if args.mirror:
        s["mirror"] = True
    if args.accent or args.theme or args.mirror:
        save_settings(s)

    try:
        engine = VideoEngine(args, s)
    except FileNotFoundError as e:
        fatal(str(e))

    engine.start()
    App(engine, s, args).run()
    print("Stopped.")


if __name__ == "__main__":
    main()
