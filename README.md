# Vigil

**Webcam-aware auto-lock for Windows.** Vigil watches your laptop camera
for your enrolled face and locks the workstation the moment you leave the
frame — or as soon as a stranger sits down. Uses face **recognition**
(not just detection) so an unrelated face doesn't keep the PC unlocked,
and handles side-angle webcam views (laptop webcam + external monitor
setup) where frontal-only detectors fail.

Built for Windows 10/11, Python 3.12, and a laptop webcam. Idle CPU
target: **under 5%** of one core on a modern laptop, **~3% of all cores**
on a 20-core desktop CPU.

---

## What it does

- Watches the webcam looking for **your** face specifically.
- If **you** leave the frame for ~6 seconds → locks the screen.
- If **a stranger** sits down (face present but not yours) → locks in ~2 seconds.
- If another app holds the camera (Zoom, Teams, OBS) → **auto-pauses** instead of locking.
- If the screen is already locked → releases the camera and stops processing.
- Runs silently in the background (`pythonw.exe`) with a tray icon.
- Global pause/unpause hotkey, defaults to **`Ctrl+Alt+P`**.
- **Auto-installs** three shortcuts + a Run-key entry on first run:
  Startup folder (login), Start Menu (searchable), Desktop
  (double-click + pin-to-taskbar), and `HKCU\...\Run\Vigil`
  (redundant login channel — see `Autostart` section).

## What it is NOT

A **security boundary**. A printed photo or a phone screen held to the
camera will defeat face recognition — this project has no liveness
detection. Treat this as a convenience auto-lock, not an authentication
system. See `Threat model` below.

---

## Install

Requires **Python 3.12** on Windows x64.

```powershell
# From the repo folder:
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The install pulls in [InsightFace](https://github.com/deepinsight/insightface)
(RetinaFace detector + ArcFace recognition) plus ONNX Runtime. No C++
compiler required — everything ships as prebuilt wheels. On the **first
run**, InsightFace downloads the `buffalo_l` model set (~280 MB) to
`~/.insightface/models/`.

> **Note on `opencv-python` vs `opencv-python-headless`:** InsightFace
> transitively pulls in `opencv-python-headless`, which is the same `cv2`
> package but built without `highgui` (so `cv2.imshow` / `namedWindow` are
> missing — enrollment needs these). `requirements.txt` pins the full
> `opencv-python` so installing in the right order should work, but if
> you ever see `cv2.error: The function is not implemented. Rebuild the
> library with Windows...`, run:
> ```powershell
> pip uninstall -y opencv-python-headless
> pip install --force-reinstall --no-deps opencv-python==4.13.0.92
> ```

## First-run: enroll your face

Run this once, from a normal terminal (not `pythonw`). It opens a live
preview window.

```powershell
python enroll.py
```

Controls:
- **SPACE** — capture a snapshot (must show a detected face)
- **ESC** / **Q** — cancel without saving

It captures 5 snapshots, averages the 512-d face embeddings, and writes
the result to `%LOCALAPPDATA%\Vigil\known_face.npy`. Shift your head
slightly between captures — **including your normal working angle** — so
the averaged embedding is robust across poses.

Extras:
- `python enroll.py --test` — also runs a 10-second live match test
  showing cosine similarity per frame, and prints min/max/mean at the end.
- `python enroll.py --test-only` — skip capture; just run the match test
  against the existing saved encoding. Useful for re-checking the
  threshold after changing your setup (lighting, monitor, hair, etc.).
- `python enroll.py --list-cameras` — enumerate DirectShow cameras by
  name with their indices. Helpful on laptops with an IR/Hello camera
  and a visible-light webcam; you want the visible-light one.
- `python enroll.py --camera N` — override `CAMERA_INDEX` for this run.

## Run it

**Foreground mode** (development, console output + Ctrl+C to stop):
```powershell
python main.py --foreground
```
Add `--verbose` for `DEBUG`-level logging.

**Background mode** (no console window — the normal way to run it):
```powershell
pythonw main.py
```

You'll see a small round icon appear in the system tray:
- **green** — watching
- **yellow** — paused or camera unavailable
- **gray** — starting or screen already locked
- **red** — stopped/error

**Right-click** the icon for `Pause/Resume`, `Re-enroll face`, and `Quit`.
(Left-click does nothing on purpose — an earlier version had left-click
toggling pause, which made it too easy to silently pause by
fat-fingering the tray icon.)

## Pause/resume hotkey

Default: **`Ctrl+Alt+P`** — press from any window to toggle monitoring.
When paused, the camera is released (so other apps can use it) and no
locking happens until you press the combo again.

Change the combo in `config.py`:

```python
PAUSE_HOTKEY = "<ctrl>+<shift>+l"   # pynput format
```

## Autostart on login + Start Menu entry

**You don't normally need to run anything** — on every daemon startup,
`main.py` idempotently ensures **four** autostart channels exist:

1. `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Vigil.lnk`
   — launched by Windows at login.
2. `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Vigil.lnk`
   — shows up in Start search and the Start Menu list so you can launch
   the daemon manually (after a tray Quit, say).
3. `%USERPROFILE%\Desktop\Vigil.lnk` (or the OneDrive Desktop equivalent
   if you have OneDrive desktop redirection)
   — visible on your Desktop so you can double-click to launch, AND
   right-click to **Pin to taskbar** / **Pin to Start**.
4. `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Vigil`
   — registry-based autostart, the same mechanism Steam, Notion, Teams,
   Spotify etc. use. **This is redundant with #1 on purpose**: the
   Startup folder is supposed to fire every login, but in field testing
   on Windows 11 it sometimes silently fails (the `.lnk` is present, but
   never gets launched — usually a `StartupApproved` cache hiccup or
   shell-init quirk). Having BOTH means we still launch even if one
   path is sabotaged. The single-instance mutex prevents the duplicate
   run from causing trouble — the second invocation pops a toast saying
   "already running" and exits.

All three shortcuts point at the venv's `pythonw.exe` with `main.py` as
the argument and use a custom icon at `%LOCALAPPDATA%\Vigil\icon.ico`
(auto-generated on first run — a green eye so it's distinguishable
from the generic Python icon in Start search). The Run-key value is
the same command line, just stored in the registry.

If you deleted any shortcut/entry by hand, it comes back on the next launch.

### "I clicked the Desktop icon and nothing happened"

If the daemon was already running, **the second click triggers the
running daemon to pop a tray balloon** saying "Vigil is already running"
within ~1 second (the second process drops a marker file in
`%LOCALAPPDATA%\Vigil\.notify_already_running` and exits via the
single-instance mutex; the watcher tick consumes the marker and asks
its tray icon to show the balloon). If the daemon WASN'T running, give
it ~10 seconds — InsightFace takes that long to load, then a tray
balloon will appear announcing "Vigil is active" and the green eye icon
will show up in your system tray (you may need to click the up-arrow in
the notification area to see hidden icons).

> Earlier versions tried to pop the balloon directly from the second
> process via either `MessageBoxW` (silently swallowed by some installed
> security tools) or a PowerShell-spawned UWP toast (XML quote-escaping
> was broken — every duplicate-launch call failed inside its own except
> handler). The current marker-file approach reuses the same `pystray`
> notification path that fires the "Vigil is active" first-launch
> balloon, so it works on every machine that the first-launch balloon
> works on.

### Putting the icon on the taskbar or Start tiles

Microsoft disabled programmatic Pin-to-Taskbar in Windows 10 ~1607 (to
stop bloatware auto-pinning itself), so this project doesn't try to.
The Desktop shortcut exists for exactly this purpose: **right-click the
`Vigil` shortcut on your Desktop** and choose:

- **Pin to taskbar** — icon sits next to the Start button, always visible
- **Pin to Start**  — icon becomes a tile on your Start menu

Both use the same `.lnk`, so the custom eye icon carries through.
Clicking the pinned icon launches the daemon — the tray icon then
indicates its state.

### Manual control

```powershell
python main.py --install-autostart    # re-install all 3 shortcuts + Run key
python main.py --uninstall-autostart  # remove all 3 + Run key
python main.py --autostart-status     # show the state of each
python main.py --no-autoinstall       # run the daemon without auto-healing
```

### How to actually disable autostart

Because the daemon self-installs on every launch, `--uninstall-autostart`
alone is not "off" — the next time `main.py` runs it'll come back. The
real off switches are:

- **Task Manager → Startup tab → Vigil → Disable** — Windows keeps the
  shortcut/Run-key entry but doesn't run them at login. Matches every
  other startup entry in the OS. Note: Task Manager shows BOTH the `.lnk`
  and the Run-key entry as separate rows; disable both to fully suppress
  autostart.
- Run the daemon only with `--no-autoinstall`, which skips the
  idempotent check.

---

## Tuning

All tunables live in `config.py`. The ones you're most likely to change:

| Setting | Default | What it does |
|---|---|---|
| `CAMERA_INDEX` | 0 | Webcam device. Run `enroll.py --list-cameras` to see names. |
| `MATCH_THRESHOLD` | 0.4 | Cosine similarity cutoff. **Higher = stricter.** 0.4 is balanced; 0.5 is strict; 0.3 is loose. |
| `STRANGER_HARD_THRESHOLD` | 0.2 | Faces between this and `MATCH_THRESHOLD` count as "uncertain" (treated as no-face, not as a stranger). Prevents glance-at-phone / hand-on-face from triggering the fast 2-second stranger lock. |
| `DETECTION_SIZE` | 320 | RetinaFace input resolution. 320 is fast (~400ms/frame at single-thread), 640 is the default InsightFace size (~1.4s/frame). |
| `FPS_TARGET` | 1 | Upper cap on loop rate. Lowering means the loop sleeps more between detections; raising past detection-bound (>2.5) is wasted CPU. |
| `PAUSE_HOTKEY` | `<ctrl>+<alt>+p` | pynput combo string. |
| `WINDOW_SIZE` | 8 | Rolling window of observations, ~8s at FPS=1. |
| `NO_FACE_LOCK_SECONDS` | 6.0 | Seconds since the owner was last seen before locking. The clock **resets** on every owner match, so glancing down at the keyboard for a few seconds does NOT accumulate. |
| `STRANGER_LOCK_FRAMES` | 2 of 8 | Consecutive stranger frames needed to lock fast. At FPS=1, 2 = ~2s. |
| `STARTUP_GRACE_SECONDS` | 5 | Initial warm-up time with no locking. |

> Cosine similarity direction note: InsightFace's similarity is the
> opposite of dlib's old `face_distance`. With InsightFace, **higher
> similarity = better match** (+1.0 is identical, ~0.5 is a solid match,
> below ~0.2 is almost certainly a different person). So `MATCH_THRESHOLD`
> is a **floor**, not a ceiling. Don't be surprised if your live-test
> numbers are 0.6-0.8 for yourself — that's normal.

If you get **false locks** (locks while you're sitting right there):
- Check `%LOCALAPPDATA%\Vigil\vigil.log` for the lock trigger log line.
- Lower `MATCH_THRESHOLD` toward 0.35.
- Re-run `python enroll.py --test-only` to see your actual similarity
  range. If min is consistently under your threshold, retune.
- Re-enroll (`python enroll.py`) including more angles / the lighting
  you actually work under.

If you get **false accepts** (someone else's face treated as yours):
- Raise `MATCH_THRESHOLD` toward 0.5.

If detection is **too slow** and reactions feel laggy:
- Keep `DETECTION_SIZE = 320`. Bumping to 640 triples the CPU cost for
  no gain at laptop-webcam distances.
- `NO_FACE_LOCK_SECONDS` is time-based, so slow FPS doesn't change when
  it fires — only `STRANGER_LOCK_FRAMES` is sensitive to FPS. Lower it if
  you want faster stranger-lock response at low FPS.

If you get **false locks while just looking down at the keyboard**:
- Raise `NO_FACE_LOCK_SECONDS` toward 8-10 so longer look-aways are fine.
- Re-enroll with your head tilted down slightly — if the detector can
  still see enough of your face when you glance down, the clock keeps
  resetting and you never approach the threshold.

If you get **false locks while glancing at your phone / hand on face**:
- The fix already in place is `STRANGER_HARD_THRESHOLD = 0.2`. If you're
  STILL seeing fast locks during occlusion, your degraded-face similarity
  is dropping below 0.2. **Lower** `STRANGER_HARD_THRESHOLD` to 0.15 or
  0.10 (NOT raise it). Then run `python enroll.py --test-only` while
  putting your hand on your face / holding up your phone — the printed
  similarity values tell you what range to set.

## Logs

Everything is logged to `%LOCALAPPDATA%\Vigil\vigil.log` with rotation
(5 MB × 3 files). Under `pythonw.exe` this is your only debugging signal.

`--foreground --verbose` also echoes to the console at `DEBUG` level.
Expect some noise from PIL (`Importing AvifImagePlugin`, etc.) at
DEBUG level — those are `pystray`'s icon generator loading PIL plugins,
they're harmless.

## Troubleshooting

### `cv2.error: The function is not implemented. Rebuild the library with Windows...`

`opencv-python-headless` won the install race over `opencv-python`. See
the install note above for the fix.

### First run hangs on "Loading InsightFace model"

The first run downloads the `buffalo_l` model set (~280 MB) from GitHub
to `~/.insightface/models/`. On a slow connection this can take a
minute or two. Subsequent runs load from disk in ~2 seconds.

### Camera won't open

Another app is holding it. Close Zoom/Teams/OBS and retry. Or — this is
expected! — start the daemon with the other app already running and it
will sit in `CAMERA_UNAVAILABLE` (yellow icon) instead of locking. When
the other app releases the camera, the daemon resumes automatically.

### Wrong camera selected (laptop with IR camera, external camera, virtual camera, etc.)

Run:
```powershell
python enroll.py --list-cameras
```
It prints something like:
```
  0: Sony Camera (Imaging Edge)
  1: USB2.0 HD UVC WebCam       <-- this one
  2: OBS Virtual Camera
```
Pick the right index and set `CAMERA_INDEX = N` in `config.py`.

### Side-angle webcam (laptop off to the side of main monitor)

Supported — that's exactly why this project uses InsightFace (RetinaFace
+ ArcFace) instead of dlib's frontal-only HOG detector. Make sure your
enrollment snapshots include your **normal working angle**, not just a
head-on shot. Then `python enroll.py --test-only` — your min similarity
at the working angle should be well above `MATCH_THRESHOLD` (0.5+ is
typical).

### Daemon silently does nothing under pythonw

Read `%LOCALAPPDATA%\Vigil\vigil.log`. The log file is the only output
channel under pythonw, and every state transition + error is captured
there. If the log is empty, `pythonw` itself failed to launch — try
`python main.py --foreground --verbose` and see what the console says.

### It locks me out during Zoom calls

It shouldn't — `CAMERA_UNAVAILABLE` is a distinct state that skips
locking. If you're still seeing this, check the log for the lock
trigger message; it's probably `no owner in last N of M frames` because
the camera technically opened but returned blank frames. File an issue
with the log excerpt.

---

## Threat model

**Honest disclosure: this is a convenience auto-lock, not a security
control.** Specifically:

- **Spoofing:** a printed photo of you, or a phone screen showing your
  face, will pass as the owner. InsightFace does not include liveness
  detection. This project is not the right tool if you need to defend
  against an attacker with physical access and a photo of you.
- **Coverage:** if the webcam is physically covered (tape, laptop
  privacy shutter), every frame returns EMPTY and the PC will lock as
  designed — good. But if the webcam view is sufficiently dark that
  detection fails *and* another app hasn't taken the camera, you'll
  get locks even though you're sitting there. Bright enough lighting
  for detection is a requirement.
- **Side channels:** this doesn't prevent keyboard/network attacks
  while the PC is unlocked. It only removes the specific risk of
  leaving an unlocked session when you step away.

What it **does** give you:
- Removes the "I forgot to Win+L" problem.
- Locks faster than Windows' inactivity timeout.
- Locks on "someone else sat down" in ~1-2 seconds.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Entry point: logging, mutex, CLI, lifecycle. |
| `config.py` | All tunables. |
| `watcher.py` | State machine + capture + detection loop. |
| `enroll.py` | First-run: capture + average face embedding. |
| `face_engine.py` | InsightFace wrapper (detection + recognition). |
| `lock.py` | `LockWorkStation` + `OpenInputDesktop` lock probe. |
| `hotkey.py` | pynput `GlobalHotKeys` listener. |
| `tray.py` | `pystray` icon + menu. |
| `autostart.py` | Startup-folder + Start-Menu + Desktop `.lnk` install/uninstall, HKCU\Run registry key install/uninstall, icon generation. |
| `requirements.txt` | Pinned deps. |
| `README.md` | This file. |

Runtime data is kept in `%LOCALAPPDATA%\Vigil\`:
- `vigil.log` — rotating log (5 MB × 3)
- `known_face.npy` — averaged 512-d face embedding (ArcFace)
- `known_face.npy.bak` — previous encoding (auto-backup on re-enroll)
- `icon.ico` — multi-resolution app icon used for the Startup + Start-Menu
  shortcuts (generated on first run)

> **Upgrading from the pre-rename "Webcam Auto-Lock" build?** The first
> launch of Vigil silently migrates `known_face.npy` (and `icon.ico`,
> and the old `lock.log`) from `%LOCALAPPDATA%\lock\` to
> `%LOCALAPPDATA%\Vigil\`, and removes any stale `WebcamAutoLock.lnk` /
> `HKCU\Run\WebcamAutoLock` entries so Task Manager's Startup tab shows
> just one row. The old `%LOCALAPPDATA%\lock\` folder is left in place
> in case you want to nuke it manually.

And model assets in `~/.insightface/models/buffalo_l/`:
- `det_10g.onnx` — RetinaFace detector
- `w600k_r50.onnx` — ArcFace recognition (512-d embedding)
- (other `.onnx` files come with the pack but are skipped at runtime)
