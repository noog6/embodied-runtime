"""Session-local, synchronous ownership of exclusive runtime resources."""

from dataclasses import dataclass
import logging
import re


LOGGER = logging.getLogger(__name__)
MAX_RESOURCE_KEY_CHARS = 64
MAX_RESOURCE_OWNER_KIND_CHARS = 32
MAX_RESOURCE_OWNER_IDENTIFIER_CHARS = 128
_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_OWNER_KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
_OWNER_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


@dataclass(frozen=True, slots=True, order=True)
class ResourceKey:
    """A normalized, machine-oriented resource identifier."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("resource key must be a string")
        normalized = self.value.strip().lower()
        if len(normalized) > MAX_RESOURCE_KEY_CHARS:
            raise ValueError(
                f"resource key must be at most {MAX_RESOURCE_KEY_CHARS} characters"
            )
        if _KEY_PATTERN.fullmatch(normalized) is None:
            raise ValueError("resource key must be a lowercase machine identifier")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ResourceOwner:
    """Semantic identity of an actor entitled to use resources."""

    kind: str
    identifier: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str):
            raise TypeError("owner kind must be a string")
        if not isinstance(self.identifier, str):
            raise TypeError("owner identifier must be a string")
        kind = self.kind.strip().lower()
        identifier = self.identifier.strip()
        if len(kind) > MAX_RESOURCE_OWNER_KIND_CHARS:
            raise ValueError(
                "owner kind must be at most "
                f"{MAX_RESOURCE_OWNER_KIND_CHARS} characters"
            )
        if len(identifier) > MAX_RESOURCE_OWNER_IDENTIFIER_CHARS:
            raise ValueError(
                "owner identifier must be at most "
                f"{MAX_RESOURCE_OWNER_IDENTIFIER_CHARS} characters"
            )
        if _OWNER_KIND_PATTERN.fullmatch(kind) is None:
            raise ValueError("owner kind must be a lowercase machine identifier")
        if _OWNER_IDENTIFIER_PATTERN.fullmatch(identifier) is None:
            raise ValueError("owner identifier must be a machine identifier")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "identifier", identifier)

    def __str__(self) -> str:
        return f"{self.kind}:{self.identifier}"


@dataclass(frozen=True, slots=True, eq=False)
class ResourceLease:
    """An identity-sensitive handle proving one session-local ownership grant."""

    id: int
    resource: ResourceKey
    owner: ResourceOwner


class ResourceBusyError(RuntimeError):
    """Raised when an exclusive resource already has an owner."""

    def __init__(self, resource: ResourceKey, current_owner: ResourceOwner) -> None:
        self.resource = resource
        self.current_owner = current_owner
        super().__init__(f"resource {resource} is held by {current_owner}")


class InvalidResourceLeaseError(RuntimeError):
    """Raised when a lease is not the exact currently active handle."""


class ResourceArbiter:
    """Coordinate fail-fast exclusive leases within one runtime session.

    This class has no awaits and provides neither thread nor cross-process locking.
    """

    def __init__(self) -> None:
        self._leases: dict[ResourceKey, ResourceLease] = {}
        self._next_lease_id = 1

    def acquire(self, resource: ResourceKey, owner: ResourceOwner) -> ResourceLease:
        if not isinstance(resource, ResourceKey):
            raise TypeError("resource must be a ResourceKey")
        if not isinstance(owner, ResourceOwner):
            raise TypeError("owner must be a ResourceOwner")
        current = self._leases.get(resource)
        if current is not None:
            LOGGER.info(
                "[RESOURCE] resource=%s owner=%s status=busy", resource, current.owner
            )
            raise ResourceBusyError(resource, current.owner)
        lease = ResourceLease(self._next_lease_id, resource, owner)
        self._next_lease_id += 1
        self._leases[resource] = lease
        LOGGER.info(
            "[RESOURCE] resource=%s owner=%s lease=L%s status=acquired",
            resource, owner, lease.id,
        )
        return lease

    def release(self, lease: ResourceLease) -> None:
        if not isinstance(lease, ResourceLease):
            raise TypeError("lease must be a ResourceLease")
        if self._leases.get(lease.resource) is not lease:
            raise InvalidResourceLeaseError("lease is not the active resource lease")
        del self._leases[lease.resource]
        LOGGER.info(
            "[RESOURCE] resource=%s owner=%s lease=L%s status=released",
            lease.resource, lease.owner, lease.id,
        )

    def release_all(self, owner: ResourceOwner) -> tuple[ResourceLease, ...]:
        if not isinstance(owner, ResourceOwner):
            raise TypeError("owner must be a ResourceOwner")
        leases = self.leases_for(owner)
        for lease in leases:
            self.release(lease)
        return leases

    def lease_for(self, resource: ResourceKey) -> ResourceLease | None:
        if not isinstance(resource, ResourceKey):
            raise TypeError("resource must be a ResourceKey")
        return self._leases.get(resource)

    def leases_for(self, owner: ResourceOwner) -> tuple[ResourceLease, ...]:
        if not isinstance(owner, ResourceOwner):
            raise TypeError("owner must be a ResourceOwner")
        return tuple(
            lease for _, lease in sorted(self._leases.items()) if lease.owner == owner
        )
