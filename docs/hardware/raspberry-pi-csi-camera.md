# Raspberry Pi CSI camera

## Scope and boundary

The first physical camera target is a small Raspberry Pi CSI camera on Raspberry
Pi OS Lite/Trixie, initially attached to a Raspberry Pi Zero 2 W. The supported
software direction is Raspberry Pi's modern libcamera/Picamera2 stack. This
foundation performs synchronous, one-shot acquisition of a 640 x 480 JPEG only:

```text
CSI camera -> Picamera2 acquisition adapter -> encoded CameraFrame
```

Acquisition is deliberately separate from future perception, attention, and AI.
There is no continuous capture, stream, preview, video recording, camera tuning
interface, or vision model. Encoded frames are transient return values: raw frame
bytes are neither `EventBus` events nor authoritative `RuntimeState` fields.

The default runtime selection is `--camera none`. Physical selection is explicit
with `--camera picamera2`; selection never silently falls back when Picamera2 or
the device is unavailable. A file is created only by the explicit
`--camera-test OUTPUT_PATH` diagnostic or local console command
`camera capture <output_path>`.

## OS provisioning

Picamera2 is an operating-system/platform dependency, not a Python project
dependency. Raspberry Pi OS Lite's supported package direction is:

```console
sudo apt install -y --no-install-recommends python3-picamera2
```

The runtime does not run this command, invoke `apt`, `pip`, or `sudo`, modify boot
or camera configuration, or attempt OS repair. Picamera2 is normally installed
as a Raspberry Pi OS system package. Official Picamera2 guidance recommends
creating a project virtual environment that needs it with system-package access,
for example:

```console
python3 -m venv --system-site-packages .venv
```

This is documentation, not an instruction for the runtime to recreate or mutate
an existing `.venv`. That choice should be made deliberately during bench setup.

## Verification status

The dependency and API direction above are **source-verified** against the
[Raspberry Pi camera software documentation](https://www.raspberrypi.com/documentation/computers/camera_software.html)
and the [official Picamera2 repository](https://github.com/raspberrypi/picamera2).
That source verification covers provisioning and API guidance; it is distinct
from the physical acquisition verification below.

The adapter's one-shot acquisition behavior was **physically bench-verified on
Mira on 2026-08-31** with:

- Raspberry Pi Zero 2 W Rev 1.0
- Raspberry Pi OS Lite / Debian Trixie
- kernel `6.18.39+rpt-rpi-v8`
- Python 3.13.5
- OV5647 Raspberry Pi CSI camera
- Picamera2 0.3.37
- libcamera `0.7.2+rpt20260817`

`rpicam-hello --list-cameras` detected:

```text
ov5647 [2592x1944 10-bit GBRG]
```

Picamera2 was installed through Raspberry Pi OS:

```console
sudo apt install -y --no-install-recommends python3-picamera2
```

System Python then imported Picamera2 successfully. The existing project venv
initially contained:

```text
include-system-site-packages = false
```

and therefore could not see the OS-provided package. As an explicit bench/setup
action, changing that existing venv setting to:

```text
include-system-site-packages = true
```

allowed the project Python environment to import Picamera2. The runtime did not
perform this mutation and does not manage virtual environments.
`Picamera2.global_camera_info()` identified the physical OV5647.

The complete runtime suite passed on Mira:

```console
python -m unittest discover
```

with 131 tests passing. The real runtime diagnostic also succeeded:

```console
python main.py \
    --camera picamera2 \
    --diagnostics \
    --camera-test /tmp/mira-camera-test.jpg
```

Observed behavior included:

```text
[CAMERA] backend=picamera2 physical=true status=ready
[CAMERA] capture width=640 height=480 media_type=image/jpeg ...
normal camera stop
normal camera close
[APP] stopped
```

The process exited with code 0. The `file` utility verified
`/tmp/mira-camera-test.jpg` as a real 640 x 480 JPEG with EXIF manufacturer
`Raspberry Pi`, software `Picamera2`, and a camera model referencing the OV5647
device. JPEG byte count is intentionally not specified because compression size
varies with the scene.

This verifies physical, one-shot acquisition only. The architectural boundary
remains:

```text
camera acquisition -> future perception -> future semantic observation
```

Raw images remain transient resources rather than `EventBus` events or
authoritative `RuntimeState`.
