# Portable vision operator validation

These are operator procedures, separate from the fake-client automated tests.
The primary and secondary interpretation commands make paid provider calls.
The final console procedure uses Mira's physical camera and cognition provider.
No live validation of Gemini/OpenRouter, direct fallback, or camera behavior has
been performed for this change. A model name in the example is an explicit
operator selection, not discovery or evidence of image/API compatibility.

## Prepare deployment configuration and credentials

Use the merged checkout's Python 3.13+ environment with the existing `openai`
extra installed. Prepare a separate deployment file:

```sh
source .venv/bin/activate
python -m pip install -e '.[openai]'
cp config/mira-agentic.toml config/mira-vision.toml
vi config/mira-vision.toml
```

Replace the existing `[runtime]` line `vision = "openai-responses"` with
`vision = "configured"`. Append this block, keeping the other deployment settings:

```toml
[vision]
request_timeout_seconds = 30

[[vision.backends]]
backend = "openai-compatible"
model = "google/gemini-3.8-flash"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
request_timeout_seconds = 15

[[vision.backends]]
backend = "openai-responses"
model = "gpt-6.1-sol"
api_key_env = "OPENAI_API_KEY"
request_timeout_seconds = 15
```

Source the protected environment files created using [secret delivery](secrets.md):

```sh
source ~/.config/embodied-runtime/openrouter.env
source ~/.config/embodied-runtime/openai.env
```

No key belongs in TOML, command arguments, or the diagnostic scripts below.
For systemd delivery, also load the existing TTS/SMS credentials selected by your
deployment before capturing. The wizard reports presence only:

```sh
python scripts/startup_wizard.py check --config config/mira-vision.toml --capture-env
python scripts/startup_wizard.py install --config config/mira-vision.toml --capture-env --force
```

`install` updates the unit and its existing mode-0600 managed environment file;
it does not start or restart Mira. If a service is already running, updating shell
exports or its file does not change that process. Use the normal explicit startup
procedure when ready. A fresh capture requires all selected variables, even
though runtime vision can fall back when one credential is missing.

## 1. Fixed image through OpenRouter Gemini

Generate an explicit 640-by-480 PNG fixture: a red rectangle on a blue background.
This local fixture is a diagnostic input, not a camera observation. Generating it
does not access hardware or a provider:

```sh
python - <<'PY'
from pathlib import Path
import struct
import zlib

def chunk(name, data):
    return (struct.pack(">I", len(data)) + name + data
            + struct.pack(">I", zlib.crc32(name + data) & 0xffffffff))

blue, red = bytes((30, 60, 220)), bytes((220, 30, 30))
rows = []
for y in range(480):
    row = blue * 640
    if 120 <= y < 360:
        row = blue * 160 + red * 320 + blue * 160
    rows.append(b"\0" + row)
image = (b"\x89PNG\r\n\x1a\n"
         + chunk(b"IHDR", struct.pack(">IIBBBBB", 640, 480, 8, 2, 0, 0, 0))
         + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
Path("/tmp/vision-test.png").write_bytes(image)
PY
```

Create this fixed-image probe. It uses production configuration, construction,
adapters, fallback, and accounting; it bypasses capture because the input is an
explicit fixed fixture. The printed description is the bounded interpretation;
diagnostics contain only safe attempt metadata:

```sh
cat > /tmp/vision-check.py <<'PY'
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

from embodied_runtime.config import load_runtime_config
from embodied_runtime.observability import RunObservability
from embodied_runtime.perception.factory import create_visual_perception_backend
from embodied_runtime.pricing import BUILT_IN_PRICING
from embodied_runtime.sensing.camera import CameraFrame

mode = sys.argv[1]
config = load_runtime_config(Path("config/mira-vision.toml")).vision
if mode == "primary":
    config = replace(config, backends=(config.backends[0],))
    expected = "openai-compatible"
elif mode == "fallback":
    assert not os.environ.get("VISION_CHECK_MISSING_KEY")
    primary = replace(config.backends[0], api_key_env="VISION_CHECK_MISSING_KEY")
    config = replace(config, backends=(primary, config.backends[1]))
    expected = "openai-responses"
else:
    raise ValueError("choose primary or fallback")

frame = CameraFrame(Path("/tmp/vision-test.png").read_bytes(), "image/png", 640, 480,
                    time.monotonic_ns())
focus = "Describe the visible shapes and colors; state uncertainty."
observed = RunObservability(pricing=BUILT_IN_PRICING)
backend = create_visual_perception_backend("configured", config)
backend.observability = observed
result = asyncio.run(backend.interpret(frame, focus))
snapshot = observed.snapshot()
calls = snapshot["provider_calls"]
print(json.dumps(calls, indent=2))
print("Interpretation:", result.description)
accepted = [call for call in calls if call.get("accepted")]
assert len(accepted) == 1 and accepted[0]["backend"] == expected
assert snapshot["metrics"]["vision_provider_requests"] == 1
assert snapshot["metrics"]["vision_fallbacks"] == (1 if mode == "fallback" else 0)
if mode == "fallback":
    assert any(event.error == "missing_credentials" for event in observed.events())
PY
PYTHONPATH="$PWD" python /tmp/vision-check.py primary
```

Only the primary entry is eligible in this check. Inspect `accepted=true`, backend
`openai-compatible`, requested model `google/gemini-3.8-flash`, returned model,
endpoint `https://openrouter.ai`, order 1, and the interpretation. An unavailable
model, incompatible image API/output parameter, provider error, or refusal must
fail this check instead of being hidden by the secondary. Do not infer full-scene
reliability from this simple fixture or treat its interpretation as authoritative.

## 2. Controlled primary failure, same image through OpenAI

Use the same PNG and focus with the full two-entry production chain. The probe
changes only the primary's credential-variable name in its local configuration
object; it does not edit the deployment file or environment files:

```sh
env -u VISION_CHECK_MISSING_KEY PYTHONPATH="$PWD" python /tmp/vision-check.py fallback
```

The primary fails locally with `missing_credentials`, without an outbound call.
The secondary receives the same immutable `CameraFrame` and focus. Inspect one
outbound vision request, one fallback, order 2, `accepted=true`, backend
`openai-responses`, endpoint `https://api.openai.com`, requested model `gpt-6.1-sol`,
and the returned model. This is a controlled fallback check without spending a
paid primary request merely to provoke an error. Failed-response usage and real
transport-error switching are separately covered by offline fake-client tests.

## 3. Normal camera-based observe_scene on Mira

With any existing Mira runtime stopped by the operator to avoid camera contention,
start the normal console using the separate deployment file and its loaded
credentials:

```sh
python main.py --config config/mira-vision.toml --console
```

At the Mira prompt, enter:

```text
camera status
ask Use observe_scene to briefly describe the objects currently visible to the camera, and state uncertainty. Do not move the body.
quit
```

Verify that `observe_scene` was actually invoked; natural-language requests do
not guarantee a model will choose a tool. The runtime should show one capture and
one applied vision acquisition. `[VISION]` logs and `summary.json` identify the
accepted provider: primary success has one request, while an eligible primary
failure may have two requests and one fallback. Capture-time `observed_at` remains
the scene time even when fallback adds latency. Camera contention rejects the
acquisition without waiting, capturing, or contacting either provider. No Job,
resource, or acquisition limits are expanded for this check.
