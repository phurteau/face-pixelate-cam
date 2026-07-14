"""
face-pixelate-cam : virtual-camera diagnostic
=============================================
Figures out WHY the virtual camera is not showing up in OBS / Streamlabs.
Run it via diagnose.bat (which also saves the output to diagnose-log.txt).

It checks, in order:
  1. Python version + 32/64-bit
  2. That numpy / cv2 / pyvirtualcam import (and their versions)
  3. Whether Windows sees an "OBS Virtual Camera" device registered
  4. Whether pyvirtualcam can actually START the virtual camera (the real test)

The last check is the important one: if it FAILS, OBS Studio's virtual camera
driver is not installed/registered, which is why nothing appears in Streamlabs.
"""

import sys
import struct
import subprocess


def hr():
    print("-" * 60)


print("=" * 60)
print(" face-pixelate-cam : virtual camera diagnostic")
print("=" * 60)

# 1) Python -----------------------------------------------------------------
print(f"Python      : {sys.version.split()[0]}")
print(f"Architecture: {struct.calcsize('P') * 8}-bit  (needs 64-bit)")
hr()

# 2) Imports ----------------------------------------------------------------
ok_imports = True
for mod in ("numpy", "cv2", "pyvirtualcam"):
    try:
        m = __import__(mod)
        print(f"import {mod:<12} OK   version {getattr(m, '__version__', '?')}")
    except Exception as e:
        ok_imports = False
        print(f"import {mod:<12} FAILED: {e}")
hr()

# 3) Is an OBS Virtual Camera device registered on this PC? ------------------
print("Looking for camera / virtual-camera devices registered in Windows:")
try:
    ps = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.Name -match 'camera|virtual|OBS' } | "
        "Select-Object -ExpandProperty Name"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=40,
    )
    names = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if names:
        for n in names:
            flag = "  <-- OBS Virtual Camera FOUND" if "obs virtual" in n.lower() else ""
            print(f"   - {n}{flag}")
    else:
        print("   (none found)")
    if not any("obs virtual" in n.lower() for n in names):
        print("   !! No 'OBS Virtual Camera' device is registered.")
        print("      That is almost certainly the problem. Fix: install OBS")
        print("      Studio (https://obsproject.com), open it, click 'Start")
        print("      Virtual Camera' once, then 'Stop Virtual Camera', close OBS.")
except Exception as e:
    print(f"   (could not query devices: {e})")
hr()

# 4) The real test: can pyvirtualcam actually start the camera? --------------
print("Attempting to START the virtual camera (this is the decisive test)...")
if not ok_imports:
    print("   Skipped: a required package failed to import (see above).")
else:
    try:
        import numpy as np
        import pyvirtualcam
        from pyvirtualcam import PixelFormat
        with pyvirtualcam.Camera(width=1280, height=720, fps=30,
                                 fmt=PixelFormat.BGR) as cam:
            print(f"   SUCCESS -> device: {cam.device}")
            frame = np.zeros((720, 1280, 3), np.uint8)
            for _ in range(30):
                cam.send(frame)
                cam.sleep_until_next_frame()
        print("   The virtual camera started and sent frames OK.")
        print()
        print("   >>> So the CAMERA works. In Streamlabs, add a 'Video Capture")
        print("       Device' source and pick the device named above")
        print("       (usually 'OBS Virtual Camera'). Keep THIS app running")
        print("       the whole time -- the camera only exists while it runs.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print()
        print(f"   FAILED to start virtual camera: {e}")
        print("   => This is why nothing appears in OBS/Streamlabs.")
        print("      Fix: install OBS Studio, open it once, click 'Start Virtual")
        print("      Camera' then 'Stop Virtual Camera', close OBS, and re-run.")
        print("      Make sure OBS's own virtual camera is NOT running when you")
        print("      start this app (they share one camera slot).")
hr()
print("Diagnostic complete. If you're stuck, share this whole output.")
