import unittest

from embodied_runtime.hardware.base import HardwareBackend
from embodied_runtime.hardware.host import HostHardwareBackend


class HostHardwareBackendTests(unittest.TestCase):
    def test_identity_capabilities_and_idempotent_lifecycle(self) -> None:
        backend = HostHardwareBackend()

        self.assertIsInstance(backend, HardwareBackend)
        self.assertEqual(backend.identifier, "host")
        self.assertTrue(backend.is_physical)
        self.assertEqual(backend.capabilities, ())
        self.assertFalse(backend.is_running)

        backend.start()
        backend.start()
        self.assertTrue(backend.is_running)
        self.assertEqual(backend.capabilities, ())

        backend.stop()
        backend.stop()
        self.assertFalse(backend.is_running)
