# embodied-runtime

Reusable software runtime for embodied agents. Mira is the working name of the
initial reference/demo robot built with the runtime.

## Run the scaffold

Python 3.13 or later is required. From a fresh checkout, run:

```console
python main.py
python main.py "Good morning, Mira."
python main.py --diagnostics
```

The runtime currently loads the Mira robot profile and uses the hardware-free
virtual backend by default. Physical SunFounder Fusion HAT+ support has not yet
been implemented. See [the architecture notes](docs/architecture.md) for the
runtime's intended boundaries.
