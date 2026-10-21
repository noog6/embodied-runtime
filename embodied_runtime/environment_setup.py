"""Configuration-driven Python dependency setup, without starting the runtime."""

import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


def required_extras(launch, *, with_sms=False):
    """Select only providers that the effective launch can use."""
    extras = set()
    if launch.cognition in {"openai-responses", "openai-compatible"} or launch.vision != "none":
        extras.add("openai")
    if launch.voice_enabled and launch.voice_tts in {"openai", "elevenlabs", "piper"}:
        extras.add(launch.voice_tts)
    if launch.sms_enabled or with_sms:
        extras.add("twilio")
    return sorted(extras)


def verify_imports(extras):
    modules = ["PIL", "prompt_toolkit"]
    for extra in extras:
        modules.extend({"openai": ["openai"], "elevenlabs": ["elevenlabs"],
                        "piper": ["piper"], "twilio": ["twilio", "aiohttp"]}[extra])
    for module in modules:
        importlib.import_module(module)
        print(f"Import {module}: OK")
    if "openai" in extras:
        # Exercise the same asynchronous SDK client as cognition, with a dummy
        # credential. Construction/close makes no provider request.
        import asyncio
        from openai import AsyncOpenAI

        async def check_client():
            async with AsyncOpenAI(api_key="setup-check-placeholder"):
                pass

        asyncio.run(check_client())
        print("OpenAI client initialization: OK (offline)")


def configure_environment(config, *, with_sms=False, check_only=False):
    from embodied_runtime.config import load_runtime_config, resolve_launch_configuration
    from embodied_runtime.profile import load_profile

    repository = Path(__file__).resolve().parent.parent
    launch = resolve_launch_configuration(SimpleNamespace(), load_runtime_config(Path(config)))
    load_profile(launch.profile)
    extras = required_extras(launch, with_sms=with_sms)
    print("Required provider extras: " + (", ".join(extras) or "none"), flush=True)
    if not check_only:
        target = str(repository) + (f"[{','.join(extras)}]" if extras else "")
        subprocess.run([sys.executable, "-m", "pip", "install", "-e", target], check=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    verify_imports(extras)
