# face-pixelate-cam

A portable Windows app that **pixelates only faces** in your webcam feed - the
body and background stay untouched - and publishes the result to the
**OBS / Streamlabs Virtual Camera**, so you can pick it as a camera inside
Streamlabs Desktop.

It also includes live **lighting** controls and an **experimental body‑slim**
effect.

---

## What it does

- 🟦 **Face pixelation (faces only).** Detection runs on **every frame** with
  MediaPipe, so the pixel block **follows your face** anywhere in the room and
  **resizes** as you move closer/farther. Handles **multiple faces**.
- 🛡️ **Safety‑biased tracking.** Boxes are padded and a "hold last position"
  buffer keeps faces covered during **fast motion** or **profile angles** so a
  frame with an exposed face is very unlikely.
- 💡 **Lighting:** brightness, contrast, saturation, warmth (white balance),
  gamma - all adjustable live.
- 🧍 **Body slim (experimental, off by default):** subtle horizontal squeeze of
  the person using selfie segmentation. Keep it gentle; pushing it hard looks
  fake.

---

## Requirements on your personal PC

1. **Python 3.10–3.12** (64‑bit). Install from
   <https://www.python.org/downloads/> and tick **"Add Python to PATH"**.
2. **OBS or Streamlabs Virtual Camera driver.** This ships with Streamlabs
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
| `m` | Toggle body slim (experimental) |
| `,` / `.` | Slim intensity down / up |
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
  hold window (`hold_frames` in `settings.json`). MediaPipe is weakest on full
  profiles.
- **Body slim looks warped** → it's experimental; lower intensity (`,`) or turn
  it off (`m`). Subtle is the intent.
- **Low frame rate** → turn body slim off, lower resolution
  (`run.bat --width 960 --height 540`).

---

## How the "faces only" part works

Each frame, MediaPipe returns bounding boxes for detected faces. The app
pixelates **only those rectangles** (down‑scale then nearest‑neighbor up‑scale),
copying the blocks back over the face region. Every other pixel is the original
frame, so your body and background are unchanged.

---

## License

MIT - see [LICENSE](LICENSE). Free to use, modify, and share.
