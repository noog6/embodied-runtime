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
from embodied_runtime.tasks import Task, TaskGoal, TaskStatus
from tests.test_platform import snapshot


class StaticPlatform:
    def snapshot(self):
        return snapshot()


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

    async def test_shutdown_drops_binding_without_terminating_running_snapshot(self):
        app = self.make_app()
        await app.start()
        running = app.start_task(Task("work", goal=TaskGoal("finish")))

        await app.stop()

        self.assertIs(running.status, TaskStatus.RUNNING)
        self.assertIsNone(app.current_task)
        self.assertIsNone(app.active_goal)


if __name__ == "__main__":
    unittest.main()
