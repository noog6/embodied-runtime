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
`--camera-test OUTPUT_PATH` diagnostic.

## OS provisioning

Picamera2 is an operating-system/platform dependency, not a Python project
dependency. Raspberry Pi OS Lite's supported package direction is:

```console
sudo apt install -y python3-picamera2 --no-install-recommends
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

## Verification status and bench test

The dependency and API direction above are **source-verified** against the
[Raspberry Pi camera software documentation](https://www.raspberrypi.com/documentation/computers/camera_software.html)
and the [official Picamera2 repository](https://github.com/raspberrypi/picamera2),
but this adapter is **not yet bench-verified on Mira**. First determine whether
the active Python environment sees Picamera2:

```console
python -c "from picamera2 import Picamera2; print('Picamera2 available')"
```

Then capture the single explicit diagnostic frame:

```console
python main.py \
    --camera picamera2 \
    --diagnostics \
    --camera-test /tmp/mira-camera-test.jpg
```

The expected result is normal application startup, one JPEG capture, a structured
camera status line with backend, dimensions, media type, byte count and output
path, followed by normal shutdown. `/tmp/mira-camera-test.jpg` should contain
exactly the captured JPEG bytes. An unavailable Python package or unusable CSI
device instead produces a concise operator-facing error and non-zero exit status.
