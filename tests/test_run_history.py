import io
import json
import logging
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from embodied_runtime.cli import main
from embodied_runtime.logging_config import configure_logging
from embodied_runtime.run_history import (
    MAX_METADATA_BYTES, RunDataUnavailable, RunHistory, RunMetadataError,
    UnsupportedRunSchema, canonical_run_id, discover_run_ids, grep_run_log,
    read_run_record, start_run,
)


class RunHistoryReaderTests(unittest.TestCase):
    def write_record(self, root: Path, number: int, **changes) -> Path:
        directory = root / f"R{number}"
        directory.mkdir()
        data = {
            "schema_version": 1, "run_id": f"R{number}", "run_number": number,
            "started_at": "2026-09-14T15:13:51.000-04:00",
            "ended_at": "2026-09-14T15:14:55.999-04:00",
            "status": "completed", "exit_code": 0, "profile": "mira",
            "hardware": "fusion-hat", "config_source": "config/mira.toml",
        }
        data.update(changes)
        (directory / "run.json").write_text(json.dumps(data))
        return directory

    def test_identity_discovery_is_exact_numeric_bounded_and_ignores_symlinks(self):
        self.assertEqual([canonical_run_id(value) for value in
                          ("R2", "r2", "R0", "R-1", "foo", "../R2")],
                         ["R2", "R2", None, None, None, None])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for number in range(1, 23):
                self.write_record(root, number)
            (root / "other").mkdir()
            (root / "R99").symlink_to(root / "R22", target_is_directory=True)
            identities, older = discover_run_ids(root)
            self.assertEqual(identities[0], "R22")
            self.assertEqual(identities[-1], "R3")
            self.assertEqual((len(identities), older), (20, 2))
        self.assertEqual(discover_run_ids(Path(temporary) / "absent"), ((), 0))

    def test_reader_validates_duration_schema_identity_and_final_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_record(root, 1)
            self.assertEqual(read_run_record(root, "R1").duration, "00:01:04")
            self.write_record(root, 2, status="interrupted", exit_code=130)
            self.assertEqual(read_run_record(root, "R2").duration, "00:01:04")
            self.write_record(root, 3, status="started", exit_code=None, ended_at=None)
            record = read_run_record(root, "R3")
            self.assertEqual((record.status, record.duration), ("started", "-"))
            self.write_record(root, 4, schema_version=2)
            with self.assertRaises(UnsupportedRunSchema):
                read_run_record(root, "R4")
            self.write_record(root, 5, run_number=9)
            with self.assertRaises(RunMetadataError):
                read_run_record(root, "R5")
            self.write_record(root, 6, status="completed", exit_code=1)
            with self.assertRaises(RunMetadataError):
                read_run_record(root, "R6")

    def test_reader_rejects_missing_oversized_malformed_and_symlink_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "R1").mkdir()
            with self.assertRaises(RunDataUnavailable):
                read_run_record(root, "R1")
            (root / "R1" / "run.json").write_bytes(b"x" * (MAX_METADATA_BYTES + 1))
            with self.assertRaises(RunDataUnavailable):
                read_run_record(root, "R1")
            (root / "R1" / "run.json").write_text("{")
            with self.assertRaises(RunMetadataError):
                read_run_record(root, "R1")
            target = root / "metadata"
            target.write_text("{}")
            (root / "R1" / "run.json").unlink()
            (root / "R1" / "run.json").symlink_to(target)
            with self.assertRaises(RunDataUnavailable):
                read_run_record(root, "R1")

    def test_grep_streams_literal_lines_and_rejects_log_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self.write_record(root, 1)
            (directory / "runtime.log").write_text(
                "one [APP]\nTwo app\nregex A only\n"
            )
            matches, truncated = grep_run_log(root, "R1", "[app]")
            self.assertEqual(matches, ((1, "one [APP]"),))
            self.assertFalse(truncated)
            matches, _ = grep_run_log(root, "R1", "TWO APP")
            self.assertEqual(matches, ((2, "Two app"),))
            target = root / "outside.log"
            target.write_text("secret")
            (directory / "runtime.log").unlink()
            (directory / "runtime.log").symlink_to(target)
            with self.assertRaises(RunDataUnavailable):
                grep_run_log(root, "R1", "secret")


class RunAllocationTests(unittest.TestCase):
    def start(self, root: Path, clock=lambda: "2026-09-14T13:48:12.123-04:00"):
        return start_run(
            root, profile="mira", hardware="virtual", config_source=None,
            clock=clock,
        )

    def test_empty_root_allocates_r1_and_initial_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            history = self.start(Path(temporary) / "runs")
            self.assertEqual(history.run_id, "R1")
            metadata = json.loads(history.metadata_path.read_text())
            self.assertEqual(metadata, {
                "schema_version": 1,
                "run_id": "R1",
                "run_number": 1,
                "started_at": "2026-09-14T13:48:12.123-04:00",
                "ended_at": None,
                "status": "started",
                "exit_code": None,
                "profile": "mira",
                "hardware": "virtual",
                "config_source": None,
            })

    def test_highest_number_wins_and_gaps_are_not_filled(self) -> None:
        for names, expected in ((["R1", "R2", "R9"], "R10"),
                                (["R1", "R3"], "R4")):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in names:
                    (root / name).mkdir()
                self.assertEqual(self.start(root).run_id, expected)

    def test_unrelated_names_and_non_directories_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("notes", "20260914", "run5", "RX", "R0", "R-1"):
                (root / name).mkdir()
            (root / "R99").write_text("not a directory")
            self.assertEqual(self.start(root).run_id, "R1")

    def test_existing_run_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "R1"
            old.mkdir()
            marker = old / "marker"
            marker.write_text("keep")
            self.assertEqual(self.start(root).run_id, "R2")
            self.assertEqual(marker.read_text(), "keep")

    def test_narrow_collision_retries_next_number(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.mkdir(exist_ok=True)
            original_mkdir = Path.mkdir
            collided = False

            def mkdir(path, *args, **kwargs):
                nonlocal collided
                if path == root / "R1" and not collided:
                    collided = True
                    original_mkdir(path)
                    raise FileExistsError
                return original_mkdir(path, *args, **kwargs)

            with patch.object(Path, "mkdir", mkdir):
                self.assertEqual(self.start(root).run_id, "R2")
            self.assertTrue((root / "R1").is_dir())

    def test_final_statuses_and_atomic_valid_json(self) -> None:
        for code, status in ((0, "completed"), (130, "interrupted"), (2, "failed")):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                times = iter(("2026-09-14T13:48:12.123-04:00",
                              "2026-09-19T09:17:44.128-04:00"))
                history = self.start(Path(temporary), lambda: next(times))
                self.assertEqual(history.finalize(code), status)
                metadata = json.loads(history.metadata_path.read_text())
                self.assertEqual(metadata["run_id"], "R1")
                self.assertEqual(metadata["started_at"],
                                 "2026-09-14T13:48:12.123-04:00")
                self.assertEqual(metadata["ended_at"],
                                 "2026-09-19T09:17:44.128-04:00")
                self.assertEqual(metadata["status"], status)
                self.assertEqual(metadata["exit_code"], code)
                self.assertFalse((history.directory / ".run.json.tmp").exists())

    def test_unfinalized_run_stays_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            history = self.start(Path(temporary))
            history.mark_started()
            self.assertFalse(history.abort())
            self.assertTrue(history.directory.exists())
            metadata = json.loads(history.metadata_path.read_text())
            self.assertEqual((metadata["status"], metadata["ended_at"],
                              metadata["exit_code"]), ("started", None, None))

    def test_provisional_abort_never_recursively_removes_unknown_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            history = self.start(Path(temporary))
            unknown = history.directory / "unknown"
            unknown.write_text("keep")
            self.assertFalse(history.abort())
            self.assertEqual(unknown.read_text(), "keep")
            self.assertFalse(history.metadata_path.exists())


class HistoryLoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_logging(stream=io.StringIO(), no_color=True)

    def test_file_is_plain_and_transport_filtered_when_console_is_coloured(self) -> None:
        class Tty(io.StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.log"
            console = Tty()
            with patch.dict("os.environ", {}, clear=True):
                self.assertTrue(configure_logging(stream=console, history_log=path))
            logging.getLogger("embodied_runtime.test").info("[APP] known record")
            logging.getLogger("httpx").setLevel(logging.INFO)
            logging.getLogger("httpx").info("success chatter")
            logging.getLogger("httpx").warning("transport problem")
            contents = path.read_text()
            self.assertIn("\x1b[", console.getvalue())
            self.assertNotRegex(contents, re.compile(r"\x1b\["))
            app_line = next(line for line in contents.splitlines()
                            if "[APP] known record" in line)
            self.assertRegex(app_line,
                             r"^\d{4}-\d{2}-\d{2}T.* \[APP\] known record$")
            self.assertNotIn("success chatter", contents)
            self.assertIn("transport problem", contents)

    def test_cli_persists_start_final_and_process_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
        ):
            self.assertEqual(main(["--diagnostics"], history_root=Path(temporary)), 0)
            log = (Path(temporary) / "R1" / "runtime.log").read_text()
            self.assertIn("[RUN] id=R1", log)
            self.assertIn("status=started", log)
            self.assertIn("[RUN] id=R1 status=completed exit_code=0", log)
            self.assertLess(log.index("[PROCESS] asyncio_cleanup status=completed"),
                            log.index("[RUN] id=R1 status=completed"))
            self.assertLess(log.index("[RUN] id=R1 status=completed"),
                            log.index("[PROCESS] main status=returning exit_code=0"))

    def test_cli_persists_interrupted_final_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(
                side_effect=KeyboardInterrupt
            )
        ):
            self.assertEqual(main(["--diagnostics"], history_root=Path(temporary)), 130)
            directory = Path(temporary) / "R1"
            metadata = json.loads((directory / "run.json").read_text())
            self.assertEqual(metadata["status"], "interrupted")
            self.assertEqual(metadata["exit_code"], 130)
            self.assertIn(
                "[RUN] id=R1 status=interrupted exit_code=130",
                (directory / "runtime.log").read_text(),
            )

    def test_invalid_launch_does_not_allocate_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                main(["--initiative"], history_root=Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_initialization_failure_warns_once_and_preserves_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "embodied_runtime.cli.start_run", side_effect=OSError
        ), patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
        ), self.assertLogs("embodied_runtime.cli", level="WARNING") as captured:
            self.assertEqual(
                main(["--diagnostics"], history_root=Path(temporary)), 0
            )
        self.assertEqual("\n".join(captured.output).count("[RUN] history="), 1)
        self.assertIn("provisional_cleanup=not_needed", "\n".join(captured.output))

    def test_finalization_failure_preserves_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=7)
        ), patch(
            "embodied_runtime.run_history.RunHistory.finalize", side_effect=OSError
        ):
            self.assertEqual(main(["--diagnostics"], history_root=Path(temporary)), 7)

    def test_file_handler_failure_rolls_back_provisional_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "embodied_runtime.logging_config.logging.FileHandler",
            side_effect=OSError,
        ), patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
        ), self.assertLogs("embodied_runtime.cli", level="WARNING") as captured:
            root = Path(temporary)
            self.assertEqual(main(["--diagnostics"], history_root=root), 0)
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual("\n".join(captured.output).count("[RUN] history="), 1)
        self.assertIn("provisional_cleanup=complete", "\n".join(captured.output))

    def test_initial_metadata_failure_cleans_only_provisional_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "R1"
            existing.mkdir()
            marker = existing / "marker"
            marker.write_text("keep")
            with patch.object(
                RunHistory, "_write_metadata", side_effect=OSError,
            ), patch(
                "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
            ), self.assertLogs("embodied_runtime.cli", level="WARNING"):
                self.assertEqual(main(["--diagnostics"], history_root=root), 0)
            self.assertEqual(marker.read_text(), "keep")
            self.assertFalse((root / "R2").exists())
            self.assertEqual(start_run(
                root,
                profile="mira",
                hardware="virtual",
                config_source=None,
            ).run_id, "R2")

    def test_unrelated_cli_test_does_not_touch_existing_history(self) -> None:
        with tempfile.TemporaryDirectory() as real_temporary, \
                tempfile.TemporaryDirectory() as isolated_temporary, patch(
                    "embodied_runtime.cli._run_application",
                    new=AsyncMock(return_value=0),
                ):
            real_root = Path(real_temporary)
            for name in ("R1", "R2"):
                (real_root / name).mkdir()
                (real_root / name / "marker").write_text(name)
            self.assertEqual(main(
                ["--diagnostics"], history_root=Path(isolated_temporary)
            ), 0)
            self.assertEqual(
                [(path.name, (path / "marker").read_text())
                 for path in sorted(real_root.iterdir())],
                [("R1", "R1"), ("R2", "R2")],
            )

    def test_cli_persists_failed_nonzero_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=7)
        ):
            root = Path(temporary)
            self.assertEqual(main(["--diagnostics"], history_root=root), 7)
            metadata = json.loads((root / "R1" / "run.json").read_text())
            self.assertEqual(metadata["status"], "failed")
            self.assertEqual(metadata["exit_code"], 7)

    def test_custom_history_root_reaches_application_console_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "embodied_runtime.cli._run_application", new=AsyncMock(return_value=0)
        ) as run_application:
            root = Path(temporary)
            self.assertEqual(main(["--console"], history_root=root), 0)
            self.assertEqual(run_application.await_args.args[2], root)
