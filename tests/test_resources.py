import unittest

from embodied_runtime.resources import (
    InvalidResourceLeaseError,
    ResourceArbiter,
    ResourceBusyError,
    ResourceKey,
    ResourceLease,
    ResourceOwner,
)


class ResourceValueTests(unittest.TestCase):
    def test_resource_key_normalizes_and_is_a_hashable_value(self):
        key = ResourceKey("  Audio.Microphone  ")
        self.assertEqual(key.value, "audio.microphone")
        self.assertEqual(key, ResourceKey("audio.microphone"))
        self.assertEqual({key}, {ResourceKey("audio.microphone")})

    def test_resource_key_rejects_non_machine_or_oversized_values(self):
        for value in ("", "two words", ".camera", "camera-1", "a" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ResourceKey(value)
        with self.assertRaises(TypeError):
            ResourceKey(1)  # type: ignore[arg-type]

    def test_owner_normalizes_kind_and_has_semantic_value_identity(self):
        owner = ResourceOwner(" TASK ", " A ")
        self.assertEqual(owner, ResourceOwner("task", "A"))
        self.assertEqual(str(owner), "task:A")

    def test_owner_rejects_invalid_components(self):
        for kind, identifier in (("", "a"), ("two words", "a"), ("task", ""),
                                 ("task", "two words"), ("x" * 33, "a"),
                                 ("task", "x" * 129)):
            with self.subTest(kind=kind, identifier=identifier), self.assertRaises(ValueError):
                ResourceOwner(kind, identifier)


class ResourceArbiterTests(unittest.TestCase):
    def setUp(self):
        self.arbiter = ResourceArbiter()
        self.camera = ResourceKey("camera")
        self.owner_a = ResourceOwner("task", "A")
        self.owner_b = ResourceOwner("task", "B")

    def test_acquire_and_exact_introspection(self):
        lease = self.arbiter.acquire(self.camera, self.owner_a)
        self.assertIs(lease.resource, self.camera)
        self.assertIs(lease.owner, self.owner_a)
        self.assertEqual(lease.id, 1)
        self.assertIs(self.arbiter.lease_for(self.camera), lease)
        self.assertEqual(self.arbiter.leases_for(self.owner_a), (lease,))

    def test_held_resource_rejects_other_and_same_owner(self):
        lease = self.arbiter.acquire(self.camera, self.owner_a)
        for owner in (self.owner_b, self.owner_a):
            with self.subTest(owner=owner), self.assertRaises(ResourceBusyError) as caught:
                self.arbiter.acquire(self.camera, owner)
            self.assertEqual(caught.exception.resource, self.camera)
            self.assertEqual(caught.exception.current_owner, self.owner_a)
        self.assertIs(self.arbiter.lease_for(self.camera), lease)

    def test_owners_can_hold_independent_multiple_resources(self):
        body = ResourceKey("body")
        microphone = ResourceKey("audio.microphone")
        camera = self.arbiter.acquire(self.camera, self.owner_a)
        body_lease = self.arbiter.acquire(body, self.owner_a)
        microphone_lease = self.arbiter.acquire(microphone, self.owner_b)
        self.assertEqual(self.arbiter.leases_for(self.owner_a), (body_lease, camera))
        self.assertEqual(self.arbiter.leases_for(self.owner_b), (microphone_lease,))

    def test_release_requires_exact_active_object_and_rejects_double_release(self):
        lease = self.arbiter.acquire(self.camera, self.owner_a)
        fabricated = ResourceLease(lease.id, lease.resource, lease.owner)
        with self.assertRaises(InvalidResourceLeaseError):
            self.arbiter.release(fabricated)
        self.arbiter.release(lease)
        self.assertIsNone(self.arbiter.lease_for(self.camera))
        with self.assertRaises(InvalidResourceLeaseError):
            self.arbiter.release(lease)

    def test_old_lease_cannot_release_new_lease_for_same_resource(self):
        old = self.arbiter.acquire(self.camera, self.owner_a)
        self.arbiter.release(old)
        new = self.arbiter.acquire(self.camera, self.owner_b)
        with self.assertRaises(InvalidResourceLeaseError):
            self.arbiter.release(old)
        self.assertIs(self.arbiter.lease_for(self.camera), new)

    def test_release_all_is_ordered_selective_and_empty_safe(self):
        resource_b = ResourceKey("resource_b")
        resource_a = ResourceKey("resource_a")
        other = ResourceKey("other")
        lease_b = self.arbiter.acquire(resource_b, self.owner_a)
        lease_a = self.arbiter.acquire(resource_a, self.owner_a)
        other_lease = self.arbiter.acquire(other, self.owner_b)

        released = self.arbiter.release_all(self.owner_a)

        self.assertEqual(released, (lease_a, lease_b))
        self.assertEqual(self.arbiter.leases_for(self.owner_a), ())
        self.assertIs(self.arbiter.lease_for(other), other_lease)
        self.assertEqual(self.arbiter.release_all(self.owner_a), ())


if __name__ == "__main__":
    unittest.main()
