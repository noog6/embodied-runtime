import asyncio
from datetime import UTC, datetime
import json
import unittest

from embodied_runtime.app import (
    COMPLETE_GOAL_TOOL,
    ApplicationOptions,
    RobotApplication,
)
from embodied_runtime.cognition import CognitionToolCall
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.profile import RobotProfile
from embodied_runtime.resources import ResourceArbiter, ResourceKey, ResourceOwner
from embodied_runtime.state import LifecycleState
from embodied_runtime.tasks import Task, TaskGoal, TaskStatus
from tests.test_platform import snapshot


class StaticPlatform:
    def snapshot(self):
        return snapshot()


class FailingReleaseArbiter(ResourceArbiter):
    def release_all(self, owner: ResourceOwner):
        raise RuntimeError("injected Task resource cleanup failure")


class TaskCoordinationTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, *, goal_closure: bool = False) -> RobotApplication:
        return RobotApplication(
            RobotProfile("test", "Test"),
            VirtualHardwareBackend(),
            ApplicationOptions(initiative_goal_closure_enabled=goal_closure),
            platform_provider=StaticPlatform(),
            wall_clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def test_starts_without_current_task_and_requires_running(self):
        app = self.make_app()
        self.assertIsNone(app.current_task)
        with self.assertRaisesRegex(RuntimeError, "running application"):
            app.start_task(Task("work"))

    async def test_start_installs_running_snapshot_and_exact_bound_goal(self):
        app = self.make_app()
        task_goal = TaskGoal("  determine whether anyone is present  ")
        task = Task("inspect the workshop", goal=task_goal)
        await app.start()

        with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
            running = app.start_task(task)

        self.assertIs(app.current_task, running)
        self.assertIs(running.status, TaskStatus.RUNNING)
        self.assertEqual(running.id, task.id)
        self.assertEqual(running.description, task.description)
        self.assertIs(running.goal, task_goal)
        bound_goal = app.active_goal
        self.assertIsNotNone(bound_goal)
        self.assertEqual(bound_goal.id, 1)
        self.assertEqual(bound_goal.description, task_goal.description)
        self.assertIn(
            f"[TASK] task={task.id} status=running goal=G1", "\n".join(logs.output)
        )

        completed = app.finish_task(TaskStatus.COMPLETED)
        next_goal = app.set_goal("standalone")
        self.assertEqual(next_goal.id, 2)
        self.assertIsNot(next_goal, bound_goal)
        self.assertIs(completed.goal, task_goal)
        await app.stop()

    async def test_task_without_goal_has_no_active_goal(self):
        app = self.make_app()
        await app.start()
        running = app.start_task(Task("work without an intention"))
        self.assertIs(app.current_task, running)
        self.assertIsNone(app.active_goal)
        self.assertFalse(app.clear_goal())
        with self.assertRaisesRegex(RuntimeError, "no active goal"):
            app.resolve_goal("completed")
        await app.stop()

    async def test_rejects_non_task_and_non_pending_snapshots(self):
        app = self.make_app()
        await app.start()
        with self.assertRaises(TypeError):
            app.start_task("work")  # type: ignore[arg-type]

        pending = Task("work")
        running = pending.transition_to(TaskStatus.RUNNING)
        paused = running.transition_to(TaskStatus.PAUSED)
        terminal = (
            running.transition_to(TaskStatus.COMPLETED),
            running.transition_to(TaskStatus.FAILED),
            running.transition_to(TaskStatus.STOPPED),
        )
        for task in (running, paused, *terminal):
            with self.assertRaisesRegex(ValueError, "must be pending"):
                app.start_task(task)
        await app.stop()

    async def test_rejects_second_task_and_unrelated_goal(self):
        app = self.make_app()
        await app.start()
        app.start_task(Task("first"))
        with self.assertRaisesRegex(RuntimeError, "current Task"):
            app.start_task(Task("second"))
        app.finish_task(TaskStatus.STOPPED)
        standalone = app.set_goal("standalone")
        with self.assertRaisesRegex(RuntimeError, "unrelated active goal"):
            app.start_task(Task("third"))
        self.assertIs(app.active_goal, standalone)
        await app.stop()

    async def test_standalone_goal_behavior_and_task_mutation_guards(self):
        app = self.make_app()
        await app.start()
        standalone = app.set_goal("standalone")
        self.assertIs(app.resolve_goal("completed"), standalone)

        app.start_task(Task("work", goal=TaskGoal("finish it")))
        bound_goal = app.active_goal
        with self.assertRaisesRegex(RuntimeError, "current"):
            app.set_goal("replacement")
        with self.assertRaisesRegex(RuntimeError, "Task-bound"):
            app.clear_goal()
        with self.assertRaisesRegex(RuntimeError, "Task-bound"):
            app.resolve_goal("completed")
        self.assertIs(app.active_goal, bound_goal)
        self.assertIsNotNone(app.current_task)
        await app.stop()

    async def test_current_tasks_hide_ordinary_goal_mutation_tools(self):
        for task in (
            Task("goal work", goal=TaskGoal("finish it")),
            Task("goalless work"),
        ):
            with self.subTest(has_goal=task.goal is not None):
                app = self.make_app()
                await app.start()
                app.start_task(task)

                names = {tool.name for tool in app.cognition_tools()}

                self.assertNotIn("set_goal", names)
                self.assertNotIn("resolve_goal", names)
                await app.stop()

    async def test_task_bound_goal_is_not_offered_outcome_completion(self):
        app = self.make_app(goal_closure=True)
        await app.start()
        running = app.start_task(Task("work", goal=TaskGoal("finish it")))
        goal = app.active_goal
        self.assertIsNotNone(goal)

        self.assertNotIn(COMPLETE_GOAL_TOOL, app.outcome_tools(goal, True))
        self.assertIs(app.current_task, running)
        self.assertIs(app.active_goal, goal)
        await app.stop()

    async def test_stale_task_bound_completion_is_rejected_without_mutation(self):
        app = self.make_app(goal_closure=True)
        await app.start()
        running = app.start_task(Task("work", goal=TaskGoal("finish it")))
        goal = app.active_goal
        self.assertIsNotNone(goal)

        result = app._execute_outcome_tool(
            CognitionToolCall("complete_goal", "{}"), goal, True
        )

        self.assertEqual(json.loads(result.output)["status"], "rejected")
        self.assertIs(app.current_task, running)
        self.assertIs(app.current_task.status, TaskStatus.RUNNING)
        self.assertIs(app.active_goal, goal)
        await app.stop()

    async def test_each_terminal_result_clears_binding_and_preserves_task(self):
        for status in (
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED
        ):
            with self.subTest(status=status):
                app = self.make_app()
                await app.start()
                original = Task("work", goal=TaskGoal("finish"))
                running = app.start_task(original)
                bound_goal = app.active_goal
                terminal = app.finish_task(status)
                self.assertIs(terminal.status, status)
                self.assertEqual(terminal.id, running.id)
                self.assertEqual(terminal.description, running.description)
                self.assertIs(terminal.goal, original.goal)
                self.assertIsNone(app.current_task)
                self.assertIsNone(app.active_goal)
                self.assertIsNotNone(bound_goal)
                await app.stop()

    async def test_finish_validates_current_task_and_terminal_status(self):
        app = self.make_app()
        with self.assertRaisesRegex(RuntimeError, "running application"):
            app.finish_task(TaskStatus.COMPLETED)
        await app.start()
        with self.assertRaisesRegex(RuntimeError, "no current Task"):
            app.finish_task(TaskStatus.COMPLETED)
        app.start_task(Task("work"))
        with self.assertRaises(TypeError):
            app.finish_task("completed")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            app.finish_task(TaskStatus.PAUSED)
        await app.stop()

    async def test_finishing_cancels_followup_bound_to_exact_goal(self):
        app = self.make_app()
        await app.start()
        app.start_task(Task("work", goal=TaskGoal("finish")))
        goal = app.active_goal
        self.assertIsNotNone(goal)
        pending = app.temporal.schedule(60, "check", goal)
        self.assertIs(pending.goal, goal)

        app.finish_task(TaskStatus.FAILED)

        self.assertIsNone(app.temporal.pending)
        await asyncio.sleep(0)
        await app.stop()

    async def test_pause_retains_task_and_releases_exact_goal_and_followup(self):
        now = [10.0]
        app = RobotApplication(
            RobotProfile("test", "Test"),
            VirtualHardwareBackend(),
            platform_provider=StaticPlatform(),
            monotonic_clock=lambda: now[0],
        )
        await app.start()
        original = Task("work", goal=TaskGoal("finish"))
        running = app.start_task(original)
        goal = app.active_goal
        self.assertIsNotNone(goal)
        app.temporal.schedule(60, "check", goal)

        with self.assertLogs("embodied_runtime.app", level="INFO") as logs:
            paused = app.pause_task()

        self.assertIs(paused.status, TaskStatus.PAUSED)
        self.assertEqual(paused.id, running.id)
        self.assertEqual(paused.description, running.description)
        self.assertIs(paused.goal, original.goal)
        self.assertIs(app.current_task, paused)
        self.assertIsNone(app.active_goal)
        self.assertIsNone(app._active_goal_started_monotonic)
        self.assertIsNone(app.temporal.pending)
        self.assertIn(
            f"[TASK] task={original.id} status=paused", "\n".join(logs.output)
        )
        await asyncio.sleep(0)
        await app.stop()

    async def test_goalless_task_pauses_and_resumes_without_active_goal(self):
        app = self.make_app()
        await app.start()
        running = app.start_task(Task("work"))

        paused = app.pause_task()
        resumed = app.resume_task()

        self.assertIs(paused.status, TaskStatus.PAUSED)
        self.assertIs(resumed.status, TaskStatus.RUNNING)
        self.assertEqual(resumed.id, running.id)
        self.assertIs(app.current_task, resumed)
        self.assertIsNone(app.active_goal)
        await app.stop()

    async def test_resume_creates_fresh_next_goal_and_does_not_restore_followup(self):
        app = self.make_app()
        await app.start()
        original = Task("work", goal=TaskGoal("finish"))
        running = app.start_task(original)
        first_goal = app.active_goal
        self.assertIsNotNone(first_goal)
        app.temporal.schedule(60, "check", first_goal)
        app.pause_task()

        resumed = app.resume_task()
        resumed_goal = app.active_goal

        self.assertIs(resumed.status, TaskStatus.RUNNING)
        self.assertEqual(resumed.id, running.id)
        self.assertEqual(resumed.description, running.description)
        self.assertIs(resumed.goal, original.goal)
        self.assertIs(app.current_task, resumed)
        self.assertIsNotNone(resumed_goal)
        self.assertIsNot(resumed_goal, first_goal)
        self.assertEqual(resumed_goal.description, first_goal.description)
        self.assertEqual(resumed_goal.id, first_goal.id + 1)
        self.assertIsNone(app.temporal.pending)
        await asyncio.sleep(0)
        await app.stop()

    async def test_pause_resume_require_valid_application_task_and_status(self):
        app = self.make_app()
        with self.assertRaisesRegex(RuntimeError, "running application"):
            app.pause_task()
        with self.assertRaisesRegex(RuntimeError, "running application"):
            app.resume_task()
        await app.start()
        with self.assertRaisesRegex(RuntimeError, "no current Task"):
            app.pause_task()
        with self.assertRaisesRegex(RuntimeError, "no current Task"):
            app.resume_task()
        app.start_task(Task("work"))
        with self.assertRaisesRegex(RuntimeError, "must be paused"):
            app.resume_task()
        app.pause_task()
        with self.assertRaisesRegex(RuntimeError, "must be running"):
            app.pause_task()
        await app.stop()

    async def test_resume_fails_closed_for_unrelated_active_goal(self):
        app = self.make_app()
        await app.start()
        paused = app.start_task(Task("work", goal=TaskGoal("finish")))
        paused = app.pause_task()
        unrelated = app._create_active_goal("unrelated")

        with self.assertRaisesRegex(RuntimeError, "binding is inconsistent"):
            app.resume_task()

        self.assertIs(app.current_task, paused)
        self.assertIs(app.active_goal, unrelated)
        app._active_goal = None
        app._active_goal_started_monotonic = None
        await app.stop()

    async def test_paused_task_keeps_current_intention_slot(self):
        app = self.make_app()
        await app.start()
        paused = app.start_task(Task("first"))
        paused = app.pause_task()

        with self.assertRaisesRegex(RuntimeError, "current Task"):
            app.start_task(Task("second"))
        with self.assertRaisesRegex(RuntimeError, "Task is current"):
            app.set_goal("standalone")
        self.assertIs(app.current_task, paused)
        self.assertNotIn("set_goal", {tool.name for tool in app.cognition_tools()})
        await app.stop()

    async def test_stop_task_from_running_reuses_terminal_cleanup(self):
        app = self.make_app()
        await app.start()
        app.start_task(Task("work", goal=TaskGoal("finish")))
        goal = app.active_goal
        self.assertIsNotNone(goal)
        app.temporal.schedule(60, "check", goal)

        stopped = app.stop_task()

        self.assertIs(stopped.status, TaskStatus.STOPPED)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)
        self.assertIsNone(app.temporal.pending)
        await asyncio.sleep(0)
        await app.stop()

    async def test_stop_task_from_paused_creates_no_goal(self):
        app = self.make_app()
        await app.start()
        app.start_task(Task("work", goal=TaskGoal("finish")))
        app.pause_task()
        next_goal_id = app._next_goal_id

        stopped = app.stop_task()

        self.assertIs(stopped.status, TaskStatus.STOPPED)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)
        self.assertEqual(app._next_goal_id, next_goal_id)
        await app.stop()

    async def test_stop_task_requires_current_task(self):
        app = self.make_app()
        await app.start()
        with self.assertRaisesRegex(RuntimeError, "no current Task"):
            app.stop_task()
        await app.stop()

    async def test_paused_task_cannot_complete_or_fail(self):
        app = self.make_app()
        await app.start()
        paused = app.start_task(Task("work"))
        paused = app.pause_task()
        for status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            with self.assertRaisesRegex(ValueError, "from paused"):
                app.finish_task(status)
            self.assertIs(app.current_task, paused)
        await app.stop()

    async def test_shutdown_drops_binding_without_terminating_running_snapshot(self):
        app = self.make_app()
        await app.start()
        running = app.start_task(Task("work", goal=TaskGoal("finish")))

        await app.stop()

        self.assertIs(running.status, TaskStatus.RUNNING)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)

    async def test_shutdown_drops_binding_without_terminating_paused_snapshot(self):
        app = self.make_app()
        await app.start()
        app.start_task(Task("work", goal=TaskGoal("finish")))
        paused = app.pause_task()

        await app.stop()

        self.assertIs(paused.status, TaskStatus.PAUSED)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)

    async def test_task_resource_acquisition_guards_and_stable_owner(self):
        app = self.make_app()
        camera = ResourceKey("camera")
        with self.assertRaisesRegex(RuntimeError, "running application"):
            app.acquire_task_resource(camera)
        await app.start()
        with self.assertRaisesRegex(RuntimeError, "no current Task"):
            app.acquire_task_resource(camera)
        running = app.start_task(Task("work"))
        lease = app.acquire_task_resource(camera)
        self.assertEqual(lease.owner, ResourceOwner("task", str(running.id)))
        paused = app.pause_task()
        with self.assertRaisesRegex(RuntimeError, "must be running"):
            app.acquire_task_resource(camera)
        self.assertEqual(paused.id, running.id)
        await app.stop()

    async def test_task_can_hold_and_explicitly_release_multiple_resources(self):
        app = self.make_app()
        await app.start()
        app.start_task(Task("work"))
        camera = app.acquire_task_resource(ResourceKey("camera"))
        body = app.acquire_task_resource(ResourceKey("body"))
        self.assertIs(app.resources.lease_for(camera.resource), camera)
        self.assertIs(app.resources.lease_for(body.resource), body)
        app.release_task_resource(camera)
        self.assertIsNone(app.resources.lease_for(camera.resource))
        self.assertIs(app.resources.lease_for(body.resource), body)
        await app.stop()

    async def test_task_cannot_release_another_semantic_owners_lease(self):
        app = self.make_app()
        await app.start()
        app.start_task(Task("work"))
        foreign = app.resources.acquire(
            ResourceKey("camera"), ResourceOwner("runtime", "voice")
        )
        with self.assertRaisesRegex(RuntimeError, "not owned"):
            app.release_task_resource(foreign)
        self.assertIs(app.resources.lease_for(foreign.resource), foreign)
        await app.stop()

    async def test_pause_releases_resources_and_resume_does_not_reacquire(self):
        app = self.make_app()
        await app.start()
        running = app.start_task(Task("work", goal=TaskGoal("finish")))
        camera_key = ResourceKey("camera")
        body_key = ResourceKey("body")
        old_camera = app.acquire_task_resource(camera_key)
        app.acquire_task_resource(body_key)

        app.pause_task()
        self.assertIsNone(app.resources.lease_for(camera_key))
        self.assertIsNone(app.resources.lease_for(body_key))
        resumed = app.resume_task()
        self.assertEqual(resumed.id, running.id)
        self.assertIsNone(app.resources.lease_for(camera_key))
        new_camera = app.acquire_task_resource(camera_key)
        self.assertIsNot(new_camera, old_camera)
        await app.stop()

    async def test_each_terminal_path_releases_all_task_resources(self):
        for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED):
            with self.subTest(status=status):
                app = self.make_app()
                await app.start()
                app.start_task(Task("work"))
                key = ResourceKey("camera")
                app.acquire_task_resource(key)
                if status is TaskStatus.STOPPED:
                    app.stop_task()
                else:
                    app.finish_task(status)
                self.assertIsNone(app.resources.lease_for(key))
                await app.stop()

    async def test_shutdown_releases_running_resources_without_task_transition(self):
        app = self.make_app()
        await app.start()
        running = app.start_task(Task("work"))
        key = ResourceKey("camera")
        app.acquire_task_resource(key)
        await app.stop()
        self.assertIs(running.status, TaskStatus.RUNNING)
        self.assertIsNone(app.resources.lease_for(key))

    async def test_shutdown_continues_and_reraises_task_cleanup_failure(self):
        hardware = VirtualHardwareBackend()
        app = RobotApplication(
            RobotProfile("test", "Test"), hardware,
            platform_provider=StaticPlatform(),
            resource_arbiter=FailingReleaseArbiter(),
        )
        await app.start()
        running = app.start_task(Task("work", goal=TaskGoal("finish")))
        lease = app.acquire_task_resource(ResourceKey("camera"))
        self.assertIs(app.current_task, running)
        self.assertIsNotNone(app.active_goal)

        with self.assertRaisesRegex(
            RuntimeError, "injected Task resource cleanup failure"
        ):
            await app.stop()

        self.assertIs(app.state, LifecycleState.STOPPED)
        self.assertFalse(hardware.is_running)
        self.assertIs(running.status, TaskStatus.RUNNING)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)
        self.assertIsNone(app._active_goal_started_monotonic)
        self.assertIs(app.resources.lease_for(lease.resource), lease)

    async def test_paused_shutdown_has_no_resources(self):
        app = self.make_app()
        await app.start()
        app.start_task(Task("work"))
        key = ResourceKey("camera")
        app.acquire_task_resource(key)
        paused = app.pause_task()
        await app.stop()
        self.assertIs(paused.status, TaskStatus.PAUSED)
        self.assertIsNone(app.resources.lease_for(key))

    async def test_task_cleanup_preserves_generic_owner_lease(self):
        resources = ResourceArbiter()
        app = RobotApplication(
            RobotProfile("test", "Test"), VirtualHardwareBackend(),
            platform_provider=StaticPlatform(), resource_arbiter=resources,
        )
        await app.start()
        app.start_task(Task("work"))
        task_key = ResourceKey("camera")
        other_key = ResourceKey("audio.microphone")
        app.acquire_task_resource(task_key)
        other = resources.acquire(other_key, ResourceOwner("runtime", "voice"))
        app.pause_task()
        self.assertIsNone(resources.lease_for(task_key))
        self.assertIs(resources.lease_for(other_key), other)
        await app.stop()


if __name__ == "__main__":
    unittest.main()
