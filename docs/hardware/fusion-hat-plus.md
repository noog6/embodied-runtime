# SunFounder Fusion HAT+

This is the verified integration record for the initial physical hardware
backend. The low-level hardware boundary remains separate from semantic body
control: selecting `fusion-hat` still composes `VirtualBodyBackend`.

## Source-verified facts

Research used SunFounder's authoritative [`sunfounder/fusion-hat` repository,
branch `v1`](https://github.com/sunfounder/fusion-hat/tree/v1). In that branch:

- [`fusion_hat/device.py`](https://github.com/sunfounder/fusion-hat/blob/v1/fusion_hat/device.py)
  defines the device at `/sys/class/fusion_hat/fusion_hat`.
- [`fusion_hat/pwm.py`](https://github.com/sunfounder/fusion-hat/blob/v1/fusion_hat/pwm.py)
  exposes logical/operator channel names `P0` through `P11`, while accessing
  kernel sysfs directories `pwm/pwm0` through `pwm/pwm11` and their `period`,
  `duty_cycle`, and `enable` files. The implementation and
  [`driver/`](https://github.com/sunfounder/fusion-hat/tree/v1/driver), rather
  than stale Servo docstring channel-count wording, are authoritative.
- [`fusion_hat/servo.py`](https://github.com/sunfounder/fusion-hat/blob/v1/fusion_hat/servo.py)
  configures servo PWM at 50 Hz and represents nominal center as 0 degrees,
  corresponding to about 1,500 microseconds (20,000 microsecond period).
- The repository [`README.md`](https://github.com/sunfounder/fusion-hat/blob/v1/README.md)
  and [`pyproject.toml`](https://github.com/sunfounder/fusion-hat/blob/v1/pyproject.toml)
  document the supported `fusion_hat doctor` operator diagnostic and the
  vendor package/install surface.

The kernel driver's `duty_cycle_store()` rejects duty-cycle writes while a
channel is disabled. Enabling initializes the timer/default prescaler while
the raw PWM output remains zero. Consequently, the explicit bench diagnostic
uses the source-required order: disable, enable, set the 20,000 microsecond
period, set the first non-zero 1,500 microsecond pulse, hold briefly, and
disable in cleanup. This intentionally differs from the initially proposed
disable/configure/enable order, which is incompatible with the driver ABI.

The runtime consequently implements board readiness, PWM, and one narrow
battery-voltage read from ADC channel A4. It does not expose general ADC,
battery policy, servo, motor, audio, GPIO, I2C, or safe-shutdown capabilities.
No mandatory vendor Python dependency is needed: the official
kernel driver/device-tree installation is an external Raspberry Pi OS
provisioning responsibility, and the runtime uses its sysfs ABI directly.

## Provisioning

Follow the official vendor instructions manually. Installation and a possible
reboot are OS provisioning steps. The runtime never downloads installers,
uses privilege escalation, repairs the OS, changes overlays, runs `doctor
--fix`, or imports the vendor Python package. If readiness fails, run the
read-only external diagnostic:

```console
fusion_hat doctor
# Optional additional operator information:
fusion_hat info
```

## Bench-verified facts

No Fusion HAT+, Raspberry Pi, or servo was available in the development
environment, so **no physical result is claimed**. Temporary-directory tests
verify only the software's sysfs behavior.

Human bench checklist:

1. Run `fusion_hat doctor` (and optionally `fusion_hat info`).
2. Run the non-actuating readiness check:
   `python main.py --hardware fusion-hat --diagnostics`.
3. Confirm hardware reports physical while body reports virtual.
4. Compare one read-only battery measurement with a multimeter:
   `python main.py --hardware fusion-hat --diagnostics --fusion-battery-test`.
   The `[BATTERY]` line reports `adc_raw`, A4 `adc_v`, and calculated
   `battery_v`. The calculation is `raw / 4095.0 * 3.3 * 3.0`, reflecting the
   Fusion HAT+'s 200K/100K divider. The command reads A4 once and then stops.
5. Connect one loose, unloaded bench servo only after independently checking
   its power and voltage requirements.
6. With hands and wiring clear, explicitly choose a channel and run:
   `python main.py --hardware fusion-hat --diagnostics --fusion-servo-test P0`.
7. Confirm the output is disabled after the half-second center-pulse test, and
   confirm repeating the ordinary diagnostic causes no movement.

> **Physical-output warning:** `--fusion-servo-test` causes real actuator
> output. A nominal 1,500 microsecond pulse is not guaranteed mechanically safe
> for an unknown linkage. Do not use a constrained mechanism. This diagnostic
> is deliberately not connected to `BodyState`, orientation, or reflexes.
