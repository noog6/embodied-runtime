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

At the application layer, every normal one-shot physical capture first acquires
the exclusive canonical `camera` resource. The lease is released immediately
after `CameraFrame` creation—even when capture raises—and never spans visual-model
interpretation. Contention fails immediately without waiting, retry, preemption,
or implicit same-owner reuse. Picamera2 remains a hardware adapter and has no
knowledge of the runtime arbiter. This authority boundary does not add streaming,
background vision, or multi-frame acquisition.

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

## Sensor coverage and routine capture

JPEG output size and sensor readout size are independent. A 640 x 480 readout
can be a central crop even with a wide-angle lens; scaling that readout cannot
restore missing edges. The adapter keeps the 640 x 480 JPEG and chooses coverage
from the connected camera's `sensor_modes`, without model names or resolution
tables in the runtime.

The API behavior was checked against the official
[Picamera2 guide, sections 4.2.2 and 4.2.2.3](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf),
and Picamera2 source at
[0.3.37](https://github.com/raspberrypi/picamera2/blob/v0.3.37/picamera2/picamera2.py)
(the previously recorded Mira installation) and
[0.3.31](https://github.com/raspberrypi/picamera2/blob/v0.3.31/picamera2/picamera2.py).
Picamera2 remains OS-provided and unpinned. These sources establish:

- `sensor_modes` enumerates readout `size`, `bit_depth`, and `crop_limits` in
  full-sensor coordinates. Enumeration temporarily reconfigures the camera and
  therefore happens before final configuration and startup.
- `create_still_configuration(main={"size": ...}, sensor={"output_size": ...,
  "bit_depth": ...})` requests a sensor mode independently of JPEG dimensions.
  Sensor parameters take precedence over raw-stream defaults. Existing raw
  stream and still buffer defaults are retained; no full-resolution JPEG is made.
- `camera_configuration()` reports the effective sensor parameters after
  libcamera validation. `camera_controls["ScalerCrop"]` gives minimum, maximum,
  and default crop rectangles. `capture_file(..., format="jpeg")` returns the
  captured frame's metadata, including `ScalerCrop` where supported.

Selection compares the largest centred rectangle of the output aspect ratio
within each reported crop. It prefers the widest usable coverage, then the
smallest adequate readout by pixel count; equal-size choices prefer higher bit
depth. Coverage equivalence requires at least 98% overlap on **each** axis with
the widest rectangle. This allows small alignment/subsampling edge margins,
without accepting a central crop. It also keeps an already economical automatic
mode when its coverage is equivalent. All readout candidates must be at least
as large as the output on both axes.

Representative driver metadata used in the tests is shown below. It is not a
measurement from the replacement Freenove module. The mode definitions are also
consistent with the official Raspberry Pi Linux drivers for
[IMX219](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/drivers/media/i2c/imx219.c)
and [OV5647](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/drivers/media/i2c/ov5647.c).
Actual startup metadata remains authoritative.

| Sensor | Readout | Reported crop `(x, y, width, height)` | Expected routine behavior |
| --- | --- | --- | --- |
| IMX219 | 640 x 480 | `(1000, 752, 1280, 960)` | Reject central crop |
| IMX219 | 1640 x 1232 | `(0, 0, 3280, 2464)` | Request economical full-view binned mode |
| IMX219 | 3280 x 2464 | `(0, 0, 3280, 2464)` | Same coverage, unnecessary readout cost |
| OV5647 | 640 x 480 | `(16, 0, 2560, 1920)` | Keep automatic VGA; near-full coverage |
| OV5647 | 1296 x 972 | `(0, 0, 2592, 1944)` | Full coverage, marginal edge gain over VGA |

For the IMX219 this reads about 2 MP instead of 0.3 MP, but avoids the roughly
8 MP full-resolution alternative. Sensor/ISP work and raw buffer memory may
increase relative to cropped VGA. Mode enumeration and checking the automatic
configuration also add startup work. JPEG size remains scene-dependent; output
dimensions, capture timestamp semantics, one-shot acquisition, camera resource
leases, and shutdown behavior remain unchanged. Latency and memory need physical
measurement on the Pi Zero 2 W.

Missing, incomplete, or unsupported mode metadata or sensor configuration keeps
the original automatic configuration call. If a successful explicit configuration
cannot be verified, the adapter restores automatic configuration and reports
`automatic_fallback=True`. Camera-open, enumeration I/O, configure, start, and
capture errors are not retried as compatibility fallbacks. No new configuration
option, OS change, or package installation is required.

The startup `[CAMERA]` diagnostic includes effective/requested sensor parameters,
output size, reported coverage, effective crop limits/default, and the fallback
flag/reason. The first normal capture logs the actual `ScalerCrop` and reports
`coverage=ok`, `reduced`, or `unknown`; reduced coverage emits a warning. This
uses metadata from that same JPEG capture and makes no extra exposure. `ok`
compares against the effective mode's coverage, subject to output aspect ratio;
inspect the startup fallback flag as well. It is not a measurement of lens angle.

## Physical verification after changing cameras

These checks remain **unverified for this sensor-selection change**. Earlier
OV5647 acquisition results above do not bench-verify the new selection logic.

1. With Mira stopped and powered off, attach the IMX219. Keep the camera and a
   fixed scene stationary, with identifiable markers near all four image edges.
   Record OS, Picamera2/libcamera versions and `rpicam-hello --list-cameras` output.
   Start Mira normally and inspect the startup diagnostic. Expect effective
   1640 x 1232 readout with full `(0, 0, 3280, 2464)` coverage if the module reports
   the representative metadata above, and output `(640, 480)` with no fallback.
2. Capture the fixed scene using the previous revision and this revision, keeping
   the same camera position. Compare visible edges: the IMX219 should recover
   the edges absent from the central VGA crop, allowing for the small 4:3 output
   aspect crop. Inspect the first capture's crop diagnostic as well. If a fallback
   or reduced/unknown coverage is logged, retain the logs and investigate before
   claiming full coverage.
3. Run the focused diagnostic with the normal project Python:
   `python main.py --camera picamera2 --diagnostics --camera-test /tmp/mira-imx219.jpg`.
   Check `file /tmp/mira-imx219.jpg` for a 640 x 480 JPEG and clean shutdown.
   In normal configured Mira operation, request vision interpretation and phone
   image delivery; confirm both succeed with the captured fixed scene.
4. Compare several one-shot captures on each revision under the same lighting.
   Record startup time separately from capture latency, and memory both idle
   and during capture (for example `ps -o pid,rss,vsz,cmd -p <PID>` and
   `/proc/<PID>/status`; `/usr/bin/time -v` if already available). Check for
   allocation errors, swapping, excessive latency, and memory pressure with
   Mira's other normal services running. Record actual values before accepting
   the Pi Zero 2 W trade-off.
5. Stop Mira, power off, and swap back to OV5647 **without editing code or
   configuration**. Repeat startup, fixed-scene comparison, diagnostic capture
   (using `/tmp/mira-ov5647.jpg`), vision, delivery, latency, memory, and shutdown
   checks. For the representative metadata above, expect automatic 640 x 480
   readout, crop `(16, 0, 2560, 1920)`, `automatic_coverage_sufficient`, and
   `automatic_fallback=False`. Visible edges should match the previous OV5647
   behavior; no new mandatory setting should be needed.
