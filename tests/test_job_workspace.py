import hashlib
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from embodied_runtime.jobs import (
    FilesystemJobWorkspaceStore, WorkspaceBackendError, WorkspaceConflictError,
    WorkspaceDurabilityError, WorkspaceNotFoundError, WorkspaceQuotaError,
    WorkspaceUnsafeError, WorkspaceValidationError,
    workspace_root_for_database,
)
from embodied_runtime.jobs.workspace import (
    MAX_ARTIFACT_BYTES, MAX_COMPONENT_BYTES, MAX_LIST_ENTRIES,
    MAX_READ_CHARS, MAX_WORKSPACE_FILES, MAX_WRITE_REQUEST_BYTES,
)


class JobWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "jobs-workspaces"
        self.store = FilesystemJobWorkspaceStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_root_derivation_and_absent_reads_create_no_job_directory(self):
        self.assertEqual(workspace_root_for_database(Path("data/jobs.sqlite3")),
                         Path("data/jobs-workspaces"))
        self.assertEqual(self.store.list_entries(7).entries, ())
        with self.assertRaises(WorkspaceNotFoundError):
            self.store.read(7, "missing.txt")
        self.assertFalse((self.root / "JOB7").exists())

    def test_path_validation(self):
        invalid = ("", ".", "..", "/a", "\\server", "C:/a", "a\\b", "a//b",
                   "a/", "a\0b", "a\x1fb", "a\x7fb", "a\x85b",
                   "a/" * 8 + "b", "x" * (MAX_COMPONENT_BYTES + 1),
                   "/".join(["abcd"] * 61))
        for path in invalid:
            with self.subTest(path=repr(path)), self.assertRaises(WorkspaceValidationError):
                self.store.write(1, path, "create", "x")
        self.store.write(1, "研究/😀.txt", "create", "ok")

    def test_exact_text_modes_versions_and_restart(self):
        text = "hé😀\r\nline\rlast\n"
        created = self.store.write(1, "artifacts/report.md", "create", text)
        self.assertEqual(created.content_version, hashlib.sha256(text.encode()).hexdigest())
        with self.assertRaises(WorkspaceConflictError):
            self.store.write(1, "artifacts/report.md", "create", "other")
        appended = self.store.write(1, "artifacts/report.md", "append", "終")
        replaced = self.store.write(1, "artifacts/report.md", "replace", text)
        self.assertNotEqual(appended.content_version, replaced.content_version)
        self.store.close()
        self.store = FilesystemJobWorkspaceStore(self.root)
        result = self.store.read(1, "artifacts/report.md")
        self.assertEqual(result.content, text)
        self.assertEqual(result.content_version, created.content_version)

    def test_text_rejections_and_empty_content(self):
        for text in ("bad\0text", "\ud800"):
            with self.assertRaises(WorkspaceValidationError):
                self.store.write(1, "bad.txt", "create", text)
        self.assertEqual(self.store.write(1, "empty.txt", "create", "").size_bytes, 0)
        (self.root / "JOB1" / "malformed.txt").write_bytes(b"\xff")
        with self.assertRaises(WorkspaceUnsafeError):
            self.store.read(1, "malformed.txt")

    def test_read_character_pagination(self):
        text = "😀" * (MAX_READ_CHARS + 2)
        # Grow through bounded append requests.
        self.store.write(1, "large.txt", "create", text[:4000])
        self.store.write(1, "large.txt", "append", text[4000:])
        first = self.store.read(1, "large.txt")
        self.assertEqual(len(first.content), MAX_READ_CHARS)
        self.assertTrue(first.truncated)
        self.assertEqual(first.next_offset_chars, MAX_READ_CHARS)
        last = self.store.read(1, "large.txt", MAX_READ_CHARS)
        self.assertEqual(last.content, "😀😀")
        end = self.store.read(1, "large.txt", len(text))
        self.assertEqual(end.content, "")
        with self.assertRaises(WorkspaceValidationError):
            self.store.read(1, "large.txt", len(text) + 1)

    def test_listing_is_one_level_sorted_bounded_and_cursor_bound(self):
        for index in reversed(range(MAX_LIST_ENTRIES + 1)):
            self.store.write(1, f"d/{index:03}.txt", "create", str(index))
        listing = self.store.list_entries(1, "d")
        self.assertEqual(len(listing.entries), MAX_LIST_ENTRIES)
        self.assertEqual(listing.entries[0].name, "000.txt")
        self.assertIsNotNone(listing.next_cursor)
        final = self.store.list_entries(1, "d", listing.next_cursor)
        self.assertEqual([entry.name for entry in final.entries], ["100.txt"])
        with self.assertRaises(WorkspaceValidationError):
            self.store.list_entries(2, "d", listing.next_cursor)
        forged = self.store._encode_cursor(1, "d", 999)
        with self.assertRaises(WorkspaceValidationError):
            self.store.list_entries(1, "d", forged)
        self.assertEqual([entry.name for entry in self.store.list_entries(1).entries], ["d"])

    def test_second_listing_page_hashes_only_that_page(self):
        for index in range(MAX_LIST_ENTRIES + 1):
            self.store.write(1, f"{index:03}.txt", "create", str(index))
        first = self.store.list_entries(1)
        with mock.patch.object(
            self.store, "_read_file", wraps=self.store._read_file,
        ) as read_file:
            second = self.store.list_entries(1, cursor=first.next_cursor)
        self.assertEqual([entry.name for entry in second.entries], ["100.txt"])
        self.assertEqual([call.args[1] for call in read_file.call_args_list], ["100.txt"])

    def test_limits_and_missing_modes(self):
        with self.assertRaises(WorkspaceQuotaError):
            self.store.write(1, "too-big-request", "create", "x" * (MAX_WRITE_REQUEST_BYTES + 1))
        with self.assertRaises(WorkspaceNotFoundError):
            self.store.write(2, "missing", "replace", "x")
        self.assertFalse((self.root / "JOB2").exists())
        with self.assertRaises(WorkspaceNotFoundError):
            self.store.write(2, "missing", "append", "x")
        for index in range(MAX_WORKSPACE_FILES):
            self.store.write(3, str(index), "create", "")
        with self.assertRaises(WorkspaceQuotaError):
            self.store.write(3, "overflow", "create", "")

        self.store.write(4, "artifact", "create", "")
        chunk = "x" * MAX_WRITE_REQUEST_BYTES
        for _ in range(MAX_ARTIFACT_BYTES // MAX_WRITE_REQUEST_BYTES):
            self.store.write(4, "artifact", "append", chunk)
        with self.assertRaises(WorkspaceQuotaError):
            self.store.write(4, "artifact", "append", "x")

        job5 = self.root / "JOB5"
        job5.mkdir()
        for index in range(33):
            (job5 / str(index)).write_bytes(b"x" * MAX_ARTIFACT_BYTES)
        with self.assertRaises(WorkspaceQuotaError):
            self.store.write(5, "new", "create", "x")

    def test_symlinks_special_files_directory_and_hardlinks_fail_closed(self):
        self.store.write(1, "safe/file", "create", "x")
        job = self.root / "JOB1"
        (job / "leaf-link").symlink_to("safe/file")
        (job / "parent-link").symlink_to("safe", target_is_directory=True)
        os.link(job / "safe/file", job / "hard")
        (job / "as-file").mkdir()
        os.mkfifo(job / "fifo")
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(job / "socket"))
        try:
            for path in ("leaf-link", "parent-link/file", "hard", "as-file", "fifo", "socket"):
                with self.subTest(path=path), self.assertRaises(WorkspaceUnsafeError):
                    self.store.read(1, path)
            with self.assertRaises(WorkspaceUnsafeError):
                self.store.list_entries(1)
        finally:
            sock.close()

    def test_root_and_job_symlinks_rejected(self):
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        linked = Path(self.temporary.name) / "linked"
        linked.symlink_to(other, target_is_directory=True)
        with self.assertRaises(WorkspaceUnsafeError):
            FilesystemJobWorkspaceStore(linked)
        (self.root / "JOB9").symlink_to(other, target_is_directory=True)
        with self.assertRaises(WorkspaceUnsafeError):
            self.store.list_entries(9)

    def test_only_exact_safe_temp_names_are_hidden(self):
        self.store.write(1, "shown", "create", "x")
        (self.root / "JOB1" / (".workspace-tmp-" + "a" * 32)).write_text("orphan")
        self.assertEqual([entry.name for entry in self.store.list_entries(1).entries], ["shown"])
        for index, name in enumerate((
            ".workspace-tmp-lookalike",
            ".workspace-tmp-" + "g" * 32,
            ".workspace-tmp-" + "a" * 32 + "-extra",
        ), 2):
            job = self.root / f"JOB{index}"
            job.mkdir()
            (job / name).write_text("not internal")
            with self.subTest(name=name), self.assertRaises(WorkspaceUnsafeError):
                self.store.list_entries(index)
            job_fd = self.store._open_job(index)
            assert job_fd is not None
            try:
                self.assertEqual(self.store._usage(job_fd), (1, len("not internal")))
            finally:
                os.close(job_fd)

    def test_create_publication_boundary_reopens_as_single_link_destination(self):
        self.store.write(1, "existing", "create", "x")
        real_fsync = os.fsync
        calls = 0
        def fail_after_publication(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interruption boundary")
            return real_fsync(fd)
        with mock.patch(
            "embodied_runtime.jobs.workspace.os.fsync", side_effect=fail_after_publication,
        ):
            with self.assertRaises(WorkspaceDurabilityError):
                self.store.write(1, "created", "create", "complete")
        self.store.close()
        self.store = FilesystemJobWorkspaceStore(self.root)
        self.assertEqual(self.store.read(1, "created").content, "complete")
        self.assertEqual((self.root / "JOB1" / "created").stat().st_nlink, 1)
        self.assertFalse(any(
            child.name.startswith(".workspace-tmp-")
            for child in (self.root / "JOB1").iterdir()
        ))

    def test_failed_nested_create_removes_only_new_empty_directories(self):
        real_open = os.open
        def fail_temporary(path, flags, *args, **kwargs):
            if isinstance(path, str) and path.startswith(".workspace-tmp-"):
                raise OSError("injected temporary creation failure")
            return real_open(path, flags, *args, **kwargs)
        with mock.patch(
            "embodied_runtime.jobs.workspace.os.open", side_effect=fail_temporary,
        ):
            with self.assertRaises(WorkspaceBackendError):
                self.store.write(8, "new/parents/file", "create", "content")
        self.assertFalse((self.root / "JOB8").exists())

    def test_directory_fsync_failure_reports_published(self):
        self.store.write(1, "file", "create", "old")
        real_fsync = os.fsync
        calls = 0
        def fail_directory(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected")
            return real_fsync(fd)
        with mock.patch("embodied_runtime.jobs.workspace.os.fsync", side_effect=fail_directory):
            with self.assertRaises(WorkspaceDurabilityError) as caught:
                self.store.write(1, "file", "replace", "new")
        self.assertTrue(caught.exception.published)
        self.assertFalse(caught.exception.durability_confirmed)
        self.assertEqual(self.store.read(1, "file").content, "new")

    def test_file_fsync_failure_leaves_destination_unchanged(self):
        self.store.write(1, "file", "create", "old")
        with mock.patch("embodied_runtime.jobs.workspace.os.fsync", side_effect=OSError("injected")):
            with self.assertRaises(WorkspaceBackendError):
                self.store.write(1, "file", "replace", "new")
        self.assertEqual(self.store.read(1, "file").content, "old")


if __name__ == "__main__":
    unittest.main()
