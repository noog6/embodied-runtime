"""Bounded extraction of a phone MPO's primary still at SMS intake only."""

import asyncio
import io
import random
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, MpoImagePlugin, features

from embodied_runtime.attachments import ImageAttachment, MAX_INTERACTION_IMAGE_BYTES
from embodied_runtime.jobs.workspace import WorkspaceQuotaError, WorkspaceValidationError
from embodied_runtime.sms import MediaDownloadError, TwilioMediaDownloader, TwilioSmsService
from tests.test_sms import fake_aiohttp, media_form, settings
from tests.test_workspace_images import raster


def mpo(size=(8, 6), *, orientation=None, quality=95, primary=None):
    image = primary if primary is not None else Image.new("RGB", size, "red")
    output = io.BytesIO()
    options = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        options["exif"] = exif
    image.save(output, format="MPO", save_all=True, quality=quality,
               append_images=[Image.new("RGB", (8, 6), "blue")], **options)
    return output.getvalue()


class SmsMpoTests(unittest.TestCase):
    def test_primary_becomes_one_standard_jpeg_and_generic_validator_stays_strict(self):
        source = mpo()
        with self.assertRaises(WorkspaceValidationError):
            ImageAttachment("image/jpeg", source).raster_metadata()
        with self.assertLogs("embodied_runtime.sms", level="INFO") as logs:
            result = TwilioMediaDownloader._primary_jpeg(source, "image/jpeg")
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(getattr(image, "n_frames", 1), 1)
            self.assertEqual(image.size, (8, 6))
            red, green, blue = image.getpixel((0, 0))
            self.assertGreater(red, 240)
            self.assertLess(blue, 15)  # the blue auxiliary picture was not selected
            self.assertNotIn("mp", image.info)
        metadata = ImageAttachment("image/jpeg", result).raster_metadata()
        self.assertEqual((metadata.width, metadata.height), (8, 6))
        self.assertIn("source_format=MPO target_format=JPEG status=normalized", "\n".join(logs.output))

    def test_ordinary_jpeg_and_png_bytes_are_unchanged(self):
        for format, media_type in (("JPEG", "image/jpeg"), ("PNG", "image/png")):
            original = raster(format)
            self.assertIs(TwilioMediaDownloader._primary_jpeg(original, media_type), original)

    def test_other_multiframe_formats_remain_rejected_instead_of_becoming_stills(self):
        formats = [("PNG", "image/png")]
        if features.check("webp"):
            formats.append(("WEBP", "image/webp"))
        for format, media_type in formats:
            with self.subTest(format=format):
                output = io.BytesIO()
                Image.new("RGB", (8, 6), "red").save(
                    output, format=format, save_all=True, duration=100,
                    append_images=[Image.new("RGB", (8, 6), "blue")],
                )
                original = output.getvalue()
                self.assertIs(TwilioMediaDownloader._primary_jpeg(original, media_type), original)
                with self.assertRaises(WorkspaceValidationError):
                    ImageAttachment(media_type, original).raster_metadata()

    def test_exif_orientation_is_applied_without_retaining_metadata(self):
        result = TwilioMediaDownloader._primary_jpeg(mpo(orientation=6), "image/jpeg")
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (6, 8))
            self.assertEqual(len(image.getexif()), 0)

    def test_auxiliary_frames_are_never_selected_for_decoding(self):
        source = mpo()
        original_seek = MpoImagePlugin.MpoImageFile.seek
        frames = []

        def primary_only(image, frame):
            frames.append(frame)
            self.assertEqual(frame, 0)
            return original_seek(image, frame)

        with patch.object(MpoImagePlugin.MpoImageFile, "seek", primary_only):
            TwilioMediaDownloader._primary_jpeg(source, "image/jpeg")
        self.assertEqual(frames, [0])

    def test_original_dimensions_and_pixel_caps_are_checked_before_decode(self):
        for size in ((4097, 1), (2049, 2048), (3024, 4032)):
            source = mpo(size)
            with self.subTest(size=size), patch.object(
                MpoImagePlugin.MpoImageFile, "load", side_effect=AssertionError("must not decode"),
            ), self.assertRaises(WorkspaceQuotaError):
                TwilioMediaDownloader._primary_jpeg(source, "image/jpeg")

    def test_input_byte_cap_is_checked_before_opening_image(self):
        with patch("PIL.Image.open", side_effect=AssertionError("must not open")):
            with self.assertRaisesRegex(MediaDownloadError, "media_too_large"):
                TwilioMediaDownloader._primary_jpeg(b"x" * (MAX_INTERACTION_IMAGE_BYTES + 1), "image/jpeg")

    def test_larger_reencoding_cannot_exceed_output_byte_cap(self):
        primary = Image.frombytes("RGB", (128, 128), random.Random(0).randbytes(128 * 128 * 3))
        source = mpo(primary=primary, quality=5)
        result = TwilioMediaDownloader._primary_jpeg(source, "image/jpeg")
        self.assertGreater(len(result), len(source))
        limit = (len(source) + len(result)) // 2
        with patch("embodied_runtime.sms.MAX_INTERACTION_IMAGE_BYTES", limit):
            with self.assertRaisesRegex(MediaDownloadError, "media_too_large"):
                TwilioMediaDownloader._primary_jpeg(source, "image/jpeg")

    def test_truncated_primary_is_not_accepted(self):
        source = mpo()
        truncated = source[:source.index(b"\xff\xd9") - 16]
        with self.assertRaises((OSError, ValueError)):
            TwilioMediaDownloader._primary_jpeg(truncated, "image/jpeg")


class SmsMpoWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_mms_delivers_one_normalized_still_to_cognition(self):
        data = mpo()

        class Content:
            async def iter_chunked(self, size):
                yield data

        response = types.SimpleNamespace(status=200, content_length=len(data),
            headers={"Content-Type": "image/jpeg"}, content=Content())

        class Context:
            async def __aenter__(self): return response
            async def __aexit__(self, *args): return False

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def get(self, *args, **kwargs): return Context()

        cognition, gateway = AsyncMock(return_value="I can see the photo."), Mock()
        service = TwilioSmsService(settings(), cognition, gateway=gateway)
        service._accepting = True
        service._worker = asyncio.create_task(service._run_worker())
        try:
            with patch.dict(sys.modules, {"aiohttp": fake_aiohttp(Session())}):
                self.assertEqual(service._accept_validated_form(media_form()).reason, "accepted")
                await asyncio.wait_for(service._inbox.join(), 2)
            cognition.assert_awaited_once()
            images = cognition.await_args.kwargs["image_attachments"]
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].media_type, "image/jpeg")
            self.assertEqual(images[0].source_message_id, "SM1")
            self.assertEqual(images[0].attachment_index, 0)
            with Image.open(io.BytesIO(images[0].data)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(getattr(image, "n_frames", 1), 1)
            gateway.send.assert_called_once()
        finally:
            await service.stop()
