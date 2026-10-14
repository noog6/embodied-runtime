"""Contained image storage: exact bytes, quotas, atomic publication and provenance."""

from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from embodied_runtime.jobs.images import MAX_IMAGE_BYTES, validate_image
from embodied_runtime.jobs.workspace import (
    FilesystemJobWorkspaceStore, ImageProvenance, MAX_ARTIFACT_BYTES,
    MAX_WORKSPACE_FILES, WorkspaceBackendError, WorkspaceConflictError,
    WorkspaceDurabilityError, WorkspaceNotFoundError, WorkspaceQuotaError,
    WorkspaceUnsafeError, WorkspaceValidationError, _IMAGE_METADATA_ATTRIBUTE,
)

CAPTURED = datetime(2025, 2, 1, 12, tzinfo=UTC)
STORED = datetime(2026, 10, 14, 12, tzinfo=UTC)


def raster(format="JPEG", color="red", size=(8, 6)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=format)
    return buffer.getvalue()


def provenance(job_id=1, captured_at=CAPTURED, run_id=9):
    return ImageProvenance("camera", "fake-camera", captured_at, STORED, job_id, run_id)


class WorkspaceImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspaces"
        self.store = FilesystemJobWorkspaceStore(self.root)
        self.a, self.b = raster(), raster("PNG", "blue")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save(self, data=None, path="photos/frame", mode="create"):
        return self.store.save_image(1, path, mode, self.a if data is None else data, provenance())

    def test_exact_bytes_metadata_listing_and_restart(self):
        for format in ("JPEG", "PNG"):
            data = raster(format)
            # Deliberately misleading extension, retained exact original encoding.
            path = "photos/" + format + ".txt"
            write = self.save(data, path)
            self.assertTrue(write.published and write.durability_confirmed)
            self.assertEqual(write.content_version, hashlib.sha256(data).hexdigest())
        self.store.close()
        self.store = FilesystemJobWorkspaceStore(self.root)
        for entry in self.store.list_entries(1, "photos").entries:
            loaded = self.store.load_image(1, entry.path)
            data = raster(entry.name.split(".")[0])
            self.assertEqual(loaded.data, data)
            self.assertEqual(loaded.provenance, provenance())
            self.assertEqual((entry.kind, entry.width, entry.height, entry.size_bytes),
                             ("image", 8, 6, len(data)))
            self.assertEqual(entry.content_version, loaded.metadata.content_version)

    def test_create_replace_upsert_and_no_append(self):
        with self.assertRaises(WorkspaceNotFoundError):
            self.save(mode="replace")
        self.save(mode="upsert")
        with self.assertRaises(WorkspaceConflictError):
            self.save()
        self.save(self.b, mode="replace")
        self.assertEqual(self.store.load_image(1, "photos/frame").data, self.b)
        self.save(mode="upsert")
        self.assertEqual(self.store.load_image(1, "photos/frame").data, self.a)
        with self.assertRaises(WorkspaceValidationError):
            self.save(mode="append")

    def test_text_and_image_operations_remain_separate(self):
        self.save()
        for mode in ("replace", "append", "upsert"):
            with self.assertRaises(WorkspaceValidationError):
                self.store.write(1, "photos/frame", mode, "do not overwrite")
        with self.assertRaises(WorkspaceValidationError):
            self.store.read(1, "photos/frame")
        self.store.write(1, "text.png", "create", "ordinary text")
        self.assertEqual(self.store.read(1, "text.png").content, "ordinary text")
        with self.assertRaises(WorkspaceValidationError):
            self.store.load_image(1, "text.png")
        for mode in ("replace", "upsert"):
            with self.assertRaises(WorkspaceValidationError):
                self.save(path="text.png", mode=mode)
        self.assertEqual(self.store.load_image(1, "photos/frame").data, self.a)

    def test_invalid_formats_truncation_animation_and_bombs(self):
        for invalid in (b"", b"\xff\xd8\xffmalformed", b"\x89PNGnot-png", self.a[:40],
                        self.b[:-20], raster("GIF"), raster("WEBP")):
            with self.subTest(prefix=invalid[:8]), self.assertRaises(WorkspaceValidationError):
                self.save(invalid)
        for size in ((4097, 1), (2049, 2049)):
            with self.subTest(size=size), self.assertRaises(WorkspaceQuotaError):
                self.save(raster("PNG", size=size))
        with patch.object(Image, "MAX_IMAGE_PIXELS", 10):
            with self.assertRaises(WorkspaceQuotaError):
                self.save(self.b)
        frames = [Image.new("RGB", (2, 2), color) for color in ("red", "blue")]
        buffer = io.BytesIO()
        frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:])
        with self.assertRaises(WorkspaceValidationError):
            self.save(buffer.getvalue())
        self.assertFalse((self.root / "JOB1").exists())

    def test_bytes_file_count_and_total_quotas_precede_decode(self):
        with patch("embodied_runtime.jobs.workspace.validate_image") as decode:
            with self.assertRaises(WorkspaceQuotaError):
                self.save(b"x" * (MAX_IMAGE_BYTES + 1))
            decode.assert_not_called()
        self.store.write(1, "existing", "create", "x")
        with patch.object(self.store, "_usage", return_value=(MAX_WORKSPACE_FILES, 1)):
            with patch("embodied_runtime.jobs.workspace.validate_image") as decode:
                with self.assertRaises(WorkspaceQuotaError):
                    self.save()
                decode.assert_not_called()
        with patch("embodied_runtime.jobs.workspace.MAX_WORKSPACE_CONTENT_BYTES", len(self.a)):
            with patch("embodied_runtime.jobs.workspace.validate_image") as decode:
                with self.assertRaises(WorkspaceQuotaError):
                    self.save()
                decode.assert_not_called()
        self.save()
        with patch.object(self.store, "_usage", return_value=(MAX_WORKSPACE_FILES, len(self.a))):
            self.save(mode="replace")  # replacement consumes no additional file

    def test_image_larger_than_text_limit_counts_towards_shared_quota(self):
        data = self.a + b"padding" * (MAX_ARTIFACT_BYTES // 7 + 1)
        self.assertGreater(len(data), MAX_ARTIFACT_BYTES)
        self.save(data)
        self.assertEqual(self.store.load_image(1, "photos/frame").data, data)
        job_fd = self.store._open_job(1)
        try:
            self.assertEqual(self.store._usage(job_fd), (1, len(data)))
        finally:
            os.close(job_fd)

    def test_containment_symlink_hardlink_and_regular_file_protection(self):
        for path in ("../escape", "/absolute", "a/../escape", "C:/escape", "a\\b"):
            with self.subTest(path=path), self.assertRaises(WorkspaceValidationError):
                self.save(path=path)
        self.save()
        external = Path(self.temp.name) / "outside"
        external.write_bytes(self.b)
        job = self.root / "JOB1"
        (job / "symlink").symlink_to(external)
        os.link(external, job / "hardlink")
        os.mkfifo(job / "fifo")
        (job / "dir-link").symlink_to(Path(self.temp.name), target_is_directory=True)
        for path in ("symlink", "hardlink", "fifo", "dir-link/outside"):
            for operation in (lambda: self.store.load_image(1, path),
                              lambda: self.save(path=path, mode="upsert")):
                with self.subTest(path=path), self.assertRaises(WorkspaceUnsafeError):
                    operation()
        self.assertEqual(external.read_bytes(), self.b)

    def test_cross_job_metadata_and_unverifiable_external_capture_are_unknown(self):
        self.save()
        with self.assertRaises(WorkspaceValidationError):
            self.store.save_image(2, "image", "create", self.a, provenance())
        (self.root / "JOB2").mkdir()
        external = self.root / "JOB2" / "image"
        external.write_bytes(self.a)
        self.assertIsNone(self.store.load_image(2, "image").provenance)
        raw = os.getxattr(self.root / "JOB1/photos/frame", _IMAGE_METADATA_ATTRIBUTE)
        os.setxattr(external, _IMAGE_METADATA_ATTRIBUTE, raw)
        self.assertIsNone(self.store.load_image(2, "image").provenance)
        external.write_bytes(self.b)
        record = json.loads(raw); record["job_id"] = 2
        os.setxattr(external, _IMAGE_METADATA_ATTRIBUTE, json.dumps(record).encode())
        self.assertIsNone(self.store.load_image(2, "image").provenance)

    def test_replacement_publishes_matching_provenance_and_snapshot_stays_old(self):
        self.save()
        a = self.store.load_image(1, "photos/frame")
        new = ImageProvenance("unknown", None, None, STORED, 1, None)
        self.store.save_image(1, "photos/frame", "replace", self.b, new)
        b = self.store.load_image(1, "photos/frame")
        self.assertEqual((a.data, a.provenance), (self.a, provenance()))
        self.assertEqual((b.data, b.provenance), (self.b, new))
        self.assertNotEqual(a.metadata.content_version, b.metadata.content_version)

    def test_prepublication_failures_leave_both_old_bytes_and_provenance(self):
        self.save()
        for target in ("os.setxattr", "os.fsync", "os.replace"):
            with self.subTest(target=target):
                with patch("embodied_runtime.jobs.workspace." + target, side_effect=OSError("PRIVATE")):
                    with self.assertRaises(WorkspaceBackendError):
                        self.save(self.b, mode="replace")
                self.assertEqual(self.store.load_image(1, "photos/frame").data, self.a)
                self.assertEqual(self.store.load_image(1, "photos/frame").provenance, provenance())
        self.assertEqual([x.name for x in (self.root / "JOB1/photos").iterdir()], ["frame"])

    def test_directory_fsync_failure_reports_publication_with_new_provenance(self):
        self.save()
        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("PRIVATE")
            real_fsync(fd)
        with patch("embodied_runtime.jobs.workspace.os.fsync", side_effect=fail_directory):
            with self.assertRaises(WorkspaceDurabilityError) as caught:
                self.save(self.b, mode="replace")
        self.assertTrue(caught.exception.published)
        self.assertFalse(caught.exception.durability_confirmed)
        loaded = self.store.load_image(1, "photos/frame")
        self.assertEqual(loaded.data, self.b)
        self.assertEqual(loaded.provenance, provenance())
        self.assertEqual(loaded.metadata.content_version, hashlib.sha256(self.b).hexdigest())

    def test_create_and_absent_upsert_no_clobber_and_prepublication_cleanup(self):
        self.store.write(1, "keep", "create", "text")
        for mode in ("create", "upsert"):
            def race(parent, temporary, destination):
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
                os.write(fd, self.b); os.close(fd)
                original(parent, temporary, destination)
            original = self.store._publish_create
            with patch.object(self.store, "_publish_create", side_effect=race):
                with self.assertRaises(WorkspaceConflictError):
                    self.save(path=mode, mode=mode)
            self.assertEqual(self.store.load_image(1, mode).data, self.b)
            self.assertIsNone(self.store.load_image(1, mode).provenance)
        with patch("embodied_runtime.jobs.workspace.os.setxattr", side_effect=OSError("PRIVATE")):
            with self.assertRaises(WorkspaceBackendError):
                self.save(path="new/parents/image")
        self.assertFalse((self.root / "JOB1/new").exists())

    def test_missing_or_invalid_attribute_does_not_invent_capture_metadata(self):
        self.save()
        path = self.root / "JOB1/photos/frame"
        for raw in (b"{}", b"not-json", b"x" * 2049, b"[" * 1001 + b"]" * 1001):
            os.setxattr(path, _IMAGE_METADATA_ATTRIBUTE, raw)
            self.assertIsNone(self.store.load_image(1, "photos/frame").provenance)
        os.removexattr(path, _IMAGE_METADATA_ATTRIBUTE)
        self.assertIsNone(self.store.load_image(1, "photos/frame").provenance)
        with patch("embodied_runtime.jobs.workspace.os.getxattr", side_effect=OSError("PRIVATE")):
            self.assertIsNone(self.store.load_image(1, "photos/frame").provenance)

    def test_oversized_external_text_keeps_existing_text_artifact_safety_limit(self):
        self.store.write(1, "existing", "create", "text")
        (self.root / "JOB1" / "oversized.txt").write_bytes(b"text" * (MAX_ARTIFACT_BYTES // 4 + 1))
        with self.assertRaises(WorkspaceUnsafeError):
            self.store.write(1, "another", "create", "text")
