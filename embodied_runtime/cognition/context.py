"""Allow-listed, immutable grounding for one cognition request."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CognitionContext:
    """Selected primitive values copied from the current application view."""

    profile_id: str
    profile_name: str
    profile_description: str
    lifecycle: str
    platform_hostname: str | None
    platform_model: str | None
    platform_system: str | None
    platform_release: str | None
    platform_machine: str | None
    platform_python_version: str | None
    platform_uptime_seconds: float | None
    platform_load_averages: tuple[float, float, float] | None
    platform_memory_total_bytes: int | None
    platform_memory_available_bytes: int | None
    platform_cpu_temperature_celsius: float | None
    hardware_backend: str
    hardware_is_physical: bool
    hardware_capabilities: tuple[str, ...]
    body_backend: str | None
    body_is_physical: bool | None
    body_capabilities: tuple[str, ...] | None
    body_yaw_degrees: float | None
    body_pitch_degrees: float | None
    presence_status: str
    presence_source: str | None
    camera_backend: str | None
    camera_is_physical: bool | None
    camera_is_running: bool | None

    def render(self) -> str:
        """Render a stable provider-neutral instruction block."""
        lines = [
            "Runtime context",
            "The following context is supplied by the robot runtime and is authoritative",
            "at the moment this request began. Treat unknown or unavailable values",
            "literally. Do not invent missing robot or sensor state.",
            "Camera resource metadata describes availability only; cognition cannot",
            "capture, access, or see images unless image data is explicitly supplied.",
            "",
            "Robot",
            f"  id: {self.profile_id}",
            f"  name: {self.profile_name}",
            f"  description: {self.profile_description or 'unavailable'}",
            "",
            "Runtime",
            f"  lifecycle: {self.lifecycle}",
            "",
            "Platform",
        ]
        if self.platform_hostname is None:
            lines.append("  state: unavailable")
        else:
            lines.extend(
                (
                    f"  hostname: {self.platform_hostname}",
                    f"  model: {_value(self.platform_model)}",
                    f"  system: {_value(self.platform_system)}",
                    f"  release: {_value(self.platform_release)}",
                    f"  machine: {_value(self.platform_machine)}",
                    f"  python: {_value(self.platform_python_version)}",
                    f"  uptime_s: {_value(self.platform_uptime_seconds)}",
                    f"  load_averages: {_loads(self.platform_load_averages)}",
                    f"  memory_total_mib: {_mib(self.platform_memory_total_bytes)}",
                    f"  memory_available_mib: {_mib(self.platform_memory_available_bytes)}",
                    f"  cpu_temp_c: {_value(self.platform_cpu_temperature_celsius)}",
                )
            )
        lines.extend(
            (
                "",
                "Hardware",
                f"  backend: {self.hardware_backend}",
                f"  physical: {_boolean(self.hardware_is_physical)}",
                f"  capabilities: {_capabilities(self.hardware_capabilities)}",
                "",
                "Body",
            )
        )
        if self.body_backend is None:
            lines.append("  state: unavailable")
        else:
            lines.extend(
                (
                    f"  backend: {self.body_backend}",
                    f"  physical: {_boolean(self.body_is_physical)}",
                    f"  capabilities: {_capabilities(self.body_capabilities)}",
                    f"  yaw_deg: {_value(self.body_yaw_degrees)}",
                    f"  pitch_deg: {_value(self.body_pitch_degrees)}",
                )
            )
        lines.extend(
            (
                "",
                "Presence",
                f"  status: {self.presence_status}",
                f"  source: {_value(self.presence_source, missing='unknown')}",
                "",
                "Camera",
            )
        )
        if self.camera_backend is None:
            lines.append("  state: unconfigured")
        else:
            lines.extend(
                (
                    "  state: configured",
                    f"  backend: {self.camera_backend}",
                    f"  physical: {_boolean(self.camera_is_physical)}",
                    f"  running: {_boolean(self.camera_is_running)}",
                )
            )
        return "\n".join(lines)


def compose_cognition_instructions(
    context: CognitionContext, startup_prompt: str | None
) -> str:
    """Keep operator instructions distinct from machine-generated grounding."""
    rendered = context.render()
    if startup_prompt is None:
        return rendered
    return f"Operator instructions\n---------------------\n{startup_prompt}\n\n{rendered}"


def _value(value: object | None, *, missing: str = "unavailable") -> str:
    return missing if value is None else str(value)


def _boolean(value: bool | None) -> str:
    return "unknown" if value is None else str(value).lower()


def _capabilities(values: tuple[str, ...] | None) -> str:
    return "unavailable" if values is None else ", ".join(values) or "none"


def _loads(values: tuple[float, float, float] | None) -> str:
    if values is None:
        return "unavailable"
    return ", ".join(str(value) for value in values)


def _mib(value: int | None) -> str:
    return "unavailable" if value is None else f"{value / (1024 * 1024):.1f}"
