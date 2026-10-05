import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from embodied_runtime.app import ApplicationOptions, RobotApplication
from embodied_runtime.earcons import (
    EARCON_CATALOG, Earcon, EarconAttemptStatus, EarconPlayer, SPEAKER_RESOURCE,
)
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.jobs import (
    JobContinuationReadiness, JobReadinessEventType, JobRunStatus,
    JobWorkDisposition, JobWorkOutcome,
    SQLiteJobStore,
)
from embodied_runtime.profile import RobotProfile
from embodied_runtime.events import ApplicationStarted
from embodied_runtime.resources import ResourceArbiter, ResourceKey, ResourceOwner
from embodied_runtime.voice import VoiceInteraction
from tests.test_platform import snapshot


class StaticPlatform:
    def snapshot(self):
        return snapshot()


class RecordingEarcons:
    def __init__(self, *, fail=False):
        self.cues = []
        self.fail = fail

    async def play(self, cue):
        self.cues.append(Earcon(cue))
        if self.fail:
            raise RuntimeError("injected audio failure")
        return True


class RecordingOutput:
    def __init__(self, resources, *, fail=False):
        self.resources = resources
        self.fail = fail
        self.held_during_playback = False
        self.calls = 0

    async def play_wav(self, wav):
        self.calls += 1
        self.held_during_playback = self.resources.lease_for(SPEAKER_RESOURCE) is not None
        if self.fail:
            raise RuntimeError("broken output")


class EarconPlayerTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_is_complete_unique_and_bounded(self):
        self.assertEqual({item.cue for item in EARCON_CATALOG}, set(Earcon))
        self.assertEqual(len(EARCON_CATALOG), len(Earcon))
        for item in EARCON_CATALOG:
            self.assertTrue(item.meaning)
            self.assertLessEqual(len(item.meaning), 100)

    def test_new_player_has_no_fabricated_activity(self):
        snapshot = EarconPlayer(ResourceArbiter(), None).snapshot()
        self.assertIsNone(snapshot.last_attempt)
        self.assertIsNone(snapshot.last_played)
        self.assertFalse(snapshot.output_available)

    async def test_enabled_player_calls_physical_output(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        self.assertTrue(await EarconPlayer(resources, output).play(Earcon.READY))
        self.assertEqual(output.calls, 1)

    async def test_success_then_skip_preserves_last_audible_cue(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        player = EarconPlayer(resources, output)
        self.assertTrue(await player.play(Earcon.READY))
        lease = resources.acquire(SPEAKER_RESOURCE, ResourceOwner("test", "speech"))
        self.assertFalse(await player.play(Earcon.WORK_STARTED))
        resources.release(lease)
        snapshot = player.snapshot()
        self.assertEqual(snapshot.last_attempt.cue, Earcon.WORK_STARTED)
        self.assertIs(snapshot.last_attempt.status, EarconAttemptStatus.SKIPPED)
        self.assertEqual(snapshot.last_attempt.reason, "speaker_busy")
        self.assertEqual(snapshot.last_played.cue, Earcon.READY)

    async def test_failure_does_not_replace_last_played(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        player = EarconPlayer(resources, output)
        await player.play(Earcon.READY)
        output.fail = True
        self.assertFalse(await player.play(Earcon.WORK_COMPLETED))
        snapshot = player.snapshot()
        self.assertIs(snapshot.last_attempt.status, EarconAttemptStatus.FAILED)
        self.assertEqual(snapshot.last_attempt.reason, "output_error")
        self.assertEqual(snapshot.last_played.cue, Earcon.READY)

    async def test_snapshot_is_read_only_and_restart_state_is_fresh(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        player = EarconPlayer(resources, output)
        await player.play(Earcon.READY)
        before = player.snapshot()
        self.assertEqual(player.snapshot(), before)
        self.assertEqual(output.calls, 1)
        self.assertIsNone(resources.lease_for(SPEAKER_RESOURCE))
        restarted = EarconPlayer(resources, output).snapshot()
        self.assertIsNone(restarted.last_attempt)
        self.assertIsNone(restarted.last_played)

    async def test_exact_speaker_lease_surrounds_playback_and_releases_on_failure(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources, fail=True)
        player = EarconPlayer(resources, output)

        self.assertFalse(await player.play(Earcon.WORK_COMPLETED))
        self.assertTrue(output.held_during_playback)
        self.assertIsNone(resources.lease_for(SPEAKER_RESOURCE))

    async def test_busy_speaker_is_skipped_without_touching_output(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        lease = resources.acquire(SPEAKER_RESOURCE, ResourceOwner("test", "speech"))
        self.assertFalse(await EarconPlayer(resources, output).play(Earcon.READY))
        self.assertFalse(output.held_during_playback)
        self.assertIs(resources.lease_for(SPEAKER_RESOURCE), lease)

    async def test_disabled_player_does_not_acquire_speaker_or_touch_output(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        player = EarconPlayer(resources, None)
        self.assertFalse(await player.play(Earcon.READY))
        self.assertEqual(output.calls, 0)
        self.assertIsNone(resources.lease_for(SPEAKER_RESOURCE))
        snapshot = player.snapshot()
        self.assertFalse(snapshot.output_available)
        self.assertIs(snapshot.last_attempt.status, EarconAttemptStatus.SKIPPED)
        self.assertEqual(snapshot.last_attempt.reason, "output_unavailable")
        self.assertIsNone(snapshot.last_played)

    async def test_wake_engagement_uses_named_earcon_exactly_once(self):
        class VoiceInput:
            async def listen(self): return None
            async def stop_listening(self): pass
            async def close(self): pass

        class Speech:
            async def speak(self, text): pass
            async def close(self): pass

        earcons = RecordingEarcons()
        voice = VoiceInteraction(
            VoiceInput(), Speech(), lambda text: asyncio.sleep(0, result=""),
            earcons=earcons,
        )
        await voice._run("wake_word")
        self.assertEqual(earcons.cues, [Earcon.ENGAGEMENT])

    async def test_disabled_engagement_still_runs_wake_conversation(self):
        class VoiceInput:
            def __init__(self): self.listens = 0
            async def listen(self):
                self.listens += 1
                return None
            async def stop_listening(self): pass
            async def close(self): pass

        class Speech:
            async def speak(self, text): pass
            async def close(self): pass

        resources = ResourceArbiter()
        provider = VoiceInput()
        voice = VoiceInteraction(
            provider, Speech(), lambda text: asyncio.sleep(0, result=""),
            earcons=EarconPlayer(resources, None),
        )
        self.assertEqual(await voice._run("wake_word"), "Voice session closed.")
        self.assertEqual(provider.listens, 1)
        self.assertIsNone(resources.lease_for(SPEAKER_RESOURCE))


class SemanticEarconTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteJobStore(Path(self.temp.name) / "jobs.sqlite3")
        self.earcons = RecordingEarcons()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def app(self, *, earcons=None):
        return RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            ApplicationOptions(jobs_auto_continue=True),
            platform_provider=StaticPlatform(), job_store=self.store,
            wall_clock=lambda: datetime(2026, 9, 29, tzinfo=UTC),
            earcon_player=earcons or self.earcons,
        )

    async def test_ready_and_job_boundaries_are_the_only_routine_cues(self):
        job = self.store.create_job("Several internal operations")
        app = self.app()
        await app.start()
        binding = app.start_job_run(job.id)
        # Representative internal telemetry, tool/resource activity, observation,
        # and event delivery have no earcon integration point.
        app.observability.event("cognition", "request", "started")
        app.observability.event("tools", "inspect", "completed")
        lease = app.resources.acquire(
            ResourceKey("test.acquisition"), ResourceOwner("test", "tool")
        )
        app.resources.release(lease)
        app.refresh_platform_state()
        await app.events.publish(ApplicationStarted(source="test-repeated-delivery"))
        await asyncio.sleep(0)
        app.finish_job_run(JobRunStatus.COMPLETED, "done")
        await asyncio.sleep(0)

        self.assertEqual(self.earcons.cues, [
            Earcon.READY, Earcon.WORK_STARTED, Earcon.WORK_COMPLETED,
        ])
        await app.stop()

    async def test_self_inspection_exposes_semantics_and_playback_distinction(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        player = EarconPlayer(resources, output)
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=StaticPlatform(), job_store=self.store,
            resource_arbiter=resources, earcon_player=player,
        )
        await player.play(Earcon.READY)
        lease = resources.acquire(SPEAKER_RESOURCE, ResourceOwner("test", "speech"))
        await player.play(Earcon.WORK_STARTED)
        resources.release(lease)
        before = player.snapshot()

        result = app._inspect_area("earcons")
        facts = {fact.name: fact.value for fact in result.facts}

        self.assertEqual(facts["last_attempt.cue"], "work_started")
        self.assertEqual(facts["last_attempt.status"], "skipped")
        self.assertEqual(facts["last_attempt.reason"], "speaker_busy")
        self.assertEqual(facts["last_played.cue"], "ready")
        self.assertIn("ready", facts["available_cue.ready.meaning"])
        self.assertEqual(player.snapshot(), before)
        self.assertEqual(output.calls, 1)
        self.assertIsNone(resources.lease_for(SPEAKER_RESOURCE))

    async def test_only_wait_for_operator_signals_and_is_deduplicated(self):
        job = self.store.create_job("Wait correctly")
        app = self.app()
        await app.start()
        binding = app.start_job_run(job.id)
        await asyncio.sleep(0)
        base = dict(
            job_id=job.id, run_id=binding.run.id, task_id=binding.task.id,
            episode_id=1, disposition=JobWorkDisposition.CONTINUE,
            summary="waiting", response="", action=None, action_status=None,
        )
        app._arm_job_continuation(JobWorkOutcome(
            **base, readiness=JobContinuationReadiness.WAIT_FOR_EVENT,
            event_type=JobReadinessEventType.PRESENCE_CHANGED,
        ))
        await asyncio.sleep(0)
        self.assertNotIn(Earcon.NEEDS_OPERATOR, self.earcons.cues)

        # Restore only to exercise a fresh authoritative readiness transition.
        app._restore_parked_job_run()
        app._arm_job_continuation(JobWorkOutcome(
            **base, readiness=JobContinuationReadiness.WAIT_FOR_OPERATOR,
        ))
        app._signal_job_earcon_once(binding.run.id, Earcon.NEEDS_OPERATOR)
        await asyncio.sleep(0)
        self.assertEqual(self.earcons.cues.count(Earcon.NEEDS_OPERATOR), 1)
        await app.stop()

    async def test_audio_failure_does_not_fail_start_or_job_transitions(self):
        resources = ResourceArbiter()
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=StaticPlatform(), job_store=self.store,
            resource_arbiter=resources,
            earcon_player=EarconPlayer(resources, RecordingOutput(resources, fail=True)),
        )
        await app.start()
        job = self.store.create_job("Still succeeds")
        binding = app.start_job_run(job.id)
        await asyncio.sleep(0)
        finished = app.finish_job_run(JobRunStatus.COMPLETED, "done")
        await asyncio.sleep(0)
        self.assertIs(binding.run.status, JobRunStatus.RUNNING)
        self.assertIs(finished.run.status, JobRunStatus.COMPLETED)
        await app.stop()

    async def test_startup_reconciliation_does_not_replay_job_cues(self):
        job = self.store.create_job("Historical")
        run = self.store.create_run(job.id)
        self.store.transition_run(run.id, JobRunStatus.RUNNING)
        app = self.app()
        await app.start()
        self.assertEqual(self.earcons.cues, [Earcon.READY])
        await app.stop()

    async def test_muted_transitions_complete_without_physical_calls(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        # Composition disables the backend; transition owners remain unchanged.
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=StaticPlatform(), job_store=self.store,
            resource_arbiter=resources, earcon_player=EarconPlayer(resources, None),
        )
        await app.start()
        job = self.store.create_job("Silent work")
        app.start_job_run(job.id)
        finished = app.finish_job_run(JobRunStatus.COMPLETED, "done")
        await asyncio.sleep(0)
        self.assertIs(finished.run.status, JobRunStatus.COMPLETED)
        self.assertEqual(output.calls, 0)
        self.assertIsNone(resources.lease_for(SPEAKER_RESOURCE))
        await app.stop()

    async def test_fast_completion_drops_contended_cues_without_backlog(self):
        resources = ResourceArbiter()
        output = RecordingOutput(resources)
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=StaticPlatform(), job_store=self.store,
            resource_arbiter=resources, earcon_player=EarconPlayer(resources, output),
        )
        await app.start()
        output.calls = 0
        occupied = resources.acquire(
            SPEAKER_RESOURCE, ResourceOwner("test", "active_tts")
        )
        job = self.store.create_job("Fast work")
        app.start_job_run(job.id)
        finished = app.finish_job_run(JobRunStatus.COMPLETED, "done")
        await asyncio.sleep(0)
        self.assertIs(finished.run.status, JobRunStatus.COMPLETED)
        self.assertEqual(output.calls, 0)
        resources.release(occupied)
        await asyncio.sleep(0)
        self.assertEqual(output.calls, 0)
        self.assertFalse(app._earcon_tasks)
        await app.stop()
