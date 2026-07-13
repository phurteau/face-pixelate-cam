# face-pixelate-cam

A portable Windows app that **pixelates only faces** in your webcam feed - the
body and background stay untouched - and publishes the result to the
**OBS / Streamlabs Virtual Camera**, so you can pick it as a camera inside
Streamlabs Desktop.

It also includes live **lighting** controls (brightness, contrast, saturation,
warmth, gamma).

---

## What it does

- 🟦 **Face pixelation (faces only).** Detection runs on **every frame** with
  OpenCV's **YuNet** face detector, so the pixel block **follows your face**
  anywhere in the room and **resizes** as you move closer/farther. Handles
  **multiple faces**.
- 🛡️ **Safety‑biased tracking.** Boxes are padded and a "hold last position"
  buffer keeps faces covered during **fast motion** or **profile angles** so a
  frame with an exposed face is very unlikely.
- 💡 **Lighting:** brightness, contrast, saturation, warmth (white balance),
  gamma - all adjustable live.

---

## Requirements on your personal PC

1. **Python 3.9–3.14** (64‑bit). Install from
   <https://www.python.org/downloads/> and tick **"Add Python to PATH"**.
   Any current version works - including **3.13 / 3.14** - because face
   detection uses OpenCV YuNet (prebuilt wheels, no compiler, no MediaPipe).
2. **The bundled model file** `face_detection_yunet_2023mar.onnx` must stay in
   the folder next to `pixelate_cam.py` (it's ~230 KB and ships with the app).
3. **OBS or Streamlabs Virtual Camera driver.** This ships with Streamlabs
   Desktop and OBS. Since you already use Streamlabs, you're covered - the app
   sends video *through* that driver. (A folder/.exe cannot carry the driver.)

> If you ever see `could not start virtual camera`, open Streamlabs/OBS once
> (which registers the driver) and try again.

---

## Setup (do this once)

1. Copy the whole **`face-pixelate-cam`** folder to your personal PC.
2. Double‑click **`setup.bat`**. It creates a local `.venv` and installs
   everything (takes a few minutes the first time).

## Run

1. Double‑click **`run.bat`**. A **preview window** opens and the **virtual
   camera** starts.
2. In **Streamlabs Desktop**: **+ (Add Source) → Video Capture Device → Add
   → pick "OBS Virtual Camera"**.
3. Move around - faces stay pixelated. Close the preview window (press **q**) to
   stop.

### Handy launch options

| Command | Effect |
|---|---|
| `run.bat` | Default (camera 0, 1280×720). |
| `run.bat --camera 1` | Use a different webcam (try 1, 2, …). |
| `run.bat --mirror` | Selfie/mirror view. |
| `run.bat --width 1920 --height 1080 --fps 30` | Force a resolution. |
| `run.bat --no-vcam` | Preview only (test without the virtual cam). |
| `run.bat --no-preview` | Headless (virtual cam only, no window). |

---

## Hotkeys (focus the preview window)

| Key | Action |
|---|---|
| `q` / `Esc` | Quit |
| `[` / `]` | Pixel block size - smaller / larger blocks |
| `-` / `=` | Face padding - less / more safety margin |
| `b` / `B` | Brightness down / up |
| `c` / `C` | Contrast down / up |
| `s` / `S` | Saturation down / up |
| `w` / `W` | Warmth cooler / warmer |
| `g` / `G` | Gamma down / up |
| `p` | Toggle pixelation on/off (panic peek) |
| `0` | Reset lighting to neutral |
| `5` / `9` | Save / reload `settings.json` |
| `h` | Toggle the on‑screen help overlay |

Your tweaks persist to **`settings.json`** (press **5** to save). Delete that
file to return to defaults.

---

## Troubleshooting

- **"could not open camera index 0"** → another app is using the webcam, or the
  index is wrong. Close other apps or try `run.bat --camera 1`.
- **Virtual camera won't start** → open Streamlabs/OBS once to register the
  driver, then rerun. Confirm you're on 64‑bit Python.
- **Face flickers when I turn fully sideways** → increase padding (`=`) or the
  hold window (`hold_frames` in `settings.json`). Face detectors are weakest on
  full profiles; the padding + hold buffer cover the gap.
- **"YuNet model not found"** → the file `face_detection_yunet_2023mar.onnx`
  must be in the same folder as `pixelate_cam.py`. Re‑download it from the
  OpenCV Zoo if it's missing.
- **Low frame rate** → lower the resolution
  (`run.bat --width 960 --height 540`).

---

## Uninstall

This app is **portable** - nothing is installed system‑wide (no registry, no
Program Files, no Start‑menu entries). To remove it:

- **Easiest:** just delete the whole `face-pixelate-cam` folder.
- **Or run `uninstall.bat`**, which:
  1. Removes generated files (`.venv`, `settings.json`, `__pycache__`,
     `build`, `dist`, `*.spec`) - resetting the folder to just the source.
  2. Then asks if you also want to **delete the entire folder** (`y` = full
     removal, including itself).

> `uninstall.bat` does **not** remove the OBS/Streamlabs **Virtual Camera
> driver** - that belongs to Streamlabs/OBS and other apps may use it. Remove
> it from Streamlabs/OBS if you no longer want it.

---

## How the "faces only" part works

Each frame, OpenCV's YuNet detector returns bounding boxes for detected faces.
The app pixelates **only those rectangles** (down‑scale then nearest‑neighbor
up‑scale), copying the blocks back over the face region. Every other pixel is
the original frame, so your body and background are unchanged.

---

## License

MIT - see [LICENSE](LICENSE). Free to use, modify, and share.
