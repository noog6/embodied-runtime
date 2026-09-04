"""Bounded provider-neutral semantic self-inspection."""

from dataclasses import dataclass
from pathlib import Path
import shutil
import socket
from typing import Protocol


SELF_INSPECTION_AREAS = ("network", "storage", "camera", "runtime")
MAX_NETWORK_INTERFACES = 8


@dataclass(frozen=True, slots=True)
class SelfInspectionFact:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class SelfInspectionResult:
    area: str
    facts: tuple[SelfInspectionFact, ...]


class SelfInspector(Protocol):
    """Injectable owner of passive host-only inspection."""

    def inspect(self, area: str) -> SelfInspectionResult: ...


class HostSelfInspector:
    """Inspect fixed local host resources without commands or network traffic."""

    def inspect(self, area: str) -> SelfInspectionResult:
        if area == "storage":
            usage = shutil.disk_usage("/")
            ratio = usage.free / usage.total if usage.total else 0.0
            return SelfInspectionResult(area, (
                SelfInspectionFact("filesystem", "/"),
                SelfInspectionFact("total_bytes", str(usage.total)),
                SelfInspectionFact("used_bytes", str(usage.used)),
                SelfInspectionFact("free_bytes", str(usage.free)),
                SelfInspectionFact("free_ratio", f"{ratio:.4f}"),
            ))
        if area == "network":
            return self._network()
        raise ValueError("host inspector supports only network and storage")

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return "unavailable"
        return value or "unavailable"

    def _network(self) -> SelfInspectionResult:
        try:
            names = sorted({name for _, name in socket.if_nameindex()})
        except OSError:
            names = []
        default = "unavailable"
        try:
            lines = Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]
            candidates = sorted(
                fields[0] for line in lines if len(fields := line.split()) >= 4
                and fields[1] == "00000000" and int(fields[3], 16) & 0x2
            )
            if candidates:
                default = candidates[0]
        except (OSError, UnicodeError, ValueError):
            pass
        reported = names[:MAX_NETWORK_INTERFACES]
        facts = [
            SelfInspectionFact("interface_count", str(len(names))),
            SelfInspectionFact("default_route_interface", default),
        ]
        for name in reported:
            base = Path("/sys/class/net") / name
            facts.extend((
                SelfInspectionFact(f"interface.{name}.operstate", self._read_text(base / "operstate")),
                SelfInspectionFact(f"interface.{name}.carrier", self._read_text(base / "carrier")),
            ))
        return SelfInspectionResult("network", tuple(facts))
