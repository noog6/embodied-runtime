from dataclasses import FrozenInstanceError
import unittest

from embodied_runtime.attachments import (
    ImageAttachment, MAX_INTERACTION_IMAGE_BYTES,
)


class ImageAttachmentTests(unittest.TestCase):
    def test_supported_images_are_valid_runtime_values(self):
        for media_type in ("image/jpeg", "image/png", "image/webp"):
            with self.subTest(media_type=media_type):
                self.assertEqual(ImageAttachment(media_type, b"x").data, b"x")

    def test_unsupported_media_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ImageAttachment("image/gif", b"x")

    def test_data_must_be_nonempty_bounded_bytes(self):
        with self.assertRaisesRegex(TypeError, "must be bytes"):
            ImageAttachment("image/jpeg", bytearray(b"x"))  # type: ignore[arg-type]
        for data in (b"", b"x" * (MAX_INTERACTION_IMAGE_BYTES + 1)):
            with self.subTest(length=len(data)), self.assertRaisesRegex(
                ValueError, "between 1 byte and 4 MiB"
            ):
                ImageAttachment("image/jpeg", data)

    def test_attachment_remains_immutable(self):
        attachment = ImageAttachment("image/png", b"x")
        with self.assertRaises(FrozenInstanceError):
            attachment.data = b"y"  # type: ignore[misc]
