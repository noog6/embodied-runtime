from dataclasses import FrozenInstanceError
import unittest
from uuid import UUID, uuid4

from embodied_runtime.tasks import (
    InvalidTaskTransitionError,
    MAX_TASK_DESCRIPTION_CHARS,
    MAX_TASK_GOAL_DESCRIPTION_CHARS,
    Task,
    TaskGoal,
    TaskStatus,
)


class TaskTests(unittest.TestCase):
    def test_construction_creates_pending_task_with_stable_identity(self):
        task = Task("  inspect the workshop  ")

        self.assertIsInstance(task.id, UUID)
        self.assertEqual(task.description, "inspect the workshop")
        self.assertIs(task.status, TaskStatus.PENDING)
        self.assertIsNone(task.goal)
        self.assertFalse(hasattr(task, "__dict__"))

    def test_explicit_identity_and_description_validation(self):
        task_id = uuid4()
        self.assertEqual(Task("work", id=task_id).id, task_id)
        with self.assertRaises(ValueError):
            Task("work", id=UUID(int=0))
        for description in (None, 1, "", "   "):
            with self.assertRaises((TypeError, ValueError)):
                Task(description)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Task("work", id="session-1")  # type: ignore[arg-type]

    def test_description_length_boundary(self):
        maximum = "x" * MAX_TASK_DESCRIPTION_CHARS
        self.assertEqual(Task(maximum).description, maximum)
        with self.assertRaises(ValueError):
            Task("x" * (MAX_TASK_DESCRIPTION_CHARS + 1))

    def test_goal_is_normalized_value_object(self):
        goal = TaskGoal("  determine whether anyone is present  ")
        task = Task("inspect the workshop", goal=goal)

        self.assertEqual(goal.description, "determine whether anyone is present")
        self.assertEqual(goal, TaskGoal("determine whether anyone is present"))
        self.assertIs(task.goal, goal)
        self.assertFalse(hasattr(goal, "__dict__"))

    def test_goal_validation_and_length_boundary(self):
        for description in (None, 1, "", "   "):
            with self.assertRaises((TypeError, ValueError)):
                TaskGoal(description)  # type: ignore[arg-type]

        maximum = "x" * MAX_TASK_GOAL_DESCRIPTION_CHARS
        self.assertEqual(TaskGoal(maximum).description, maximum)
        with self.assertRaises(ValueError):
            TaskGoal("x" * (MAX_TASK_GOAL_DESCRIPTION_CHARS + 1))
        with self.assertRaises(TypeError):
            Task("work", goal="finish")  # type: ignore[arg-type]

    def test_pause_resume_and_complete_path(self):
        goal = TaskGoal("produce the result")
        pending = Task("work", goal=goal)
        running = pending.transition_to(TaskStatus.RUNNING)
        paused = running.transition_to(TaskStatus.PAUSED)
        resumed = paused.transition_to(TaskStatus.RUNNING)
        completed = resumed.transition_to(TaskStatus.COMPLETED)

        self.assertEqual(
            [pending.status, running.status, paused.status, resumed.status,
             completed.status],
            [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED,
             TaskStatus.RUNNING, TaskStatus.COMPLETED],
        )
        self.assertEqual({snapshot.id for snapshot in (
            pending, running, paused, resumed, completed)}, {pending.id})
        for snapshot in (pending, running, paused, resumed, completed):
            self.assertEqual(snapshot.description, pending.description)
            self.assertIs(snapshot.goal, goal)

    def test_running_can_fail(self):
        task = Task("work").transition_to(TaskStatus.RUNNING)
        self.assertIs(task.transition_to(TaskStatus.FAILED).status, TaskStatus.FAILED)

    def test_every_allowed_transition_preserves_goal(self):
        goal = TaskGoal("finish the work")
        pending = Task("work", goal=goal)
        running = pending.transition_to(TaskStatus.RUNNING)
        paused = running.transition_to(TaskStatus.PAUSED)

        transitions = (
            (pending, TaskStatus.RUNNING),
            (pending, TaskStatus.STOPPED),
            (running, TaskStatus.PAUSED),
            (running, TaskStatus.COMPLETED),
            (running, TaskStatus.FAILED),
            (running, TaskStatus.STOPPED),
            (paused, TaskStatus.RUNNING),
            (paused, TaskStatus.STOPPED),
        )
        for snapshot, target in transitions:
            transitioned = snapshot.transition_to(target)
            self.assertEqual(transitioned.id, snapshot.id)
            self.assertEqual(transitioned.description, snapshot.description)
            self.assertIs(transitioned.goal, goal)

    def test_stopping_from_nonterminal_states(self):
        pending = Task("pending")
        running = Task("running").transition_to(TaskStatus.RUNNING)
        paused = running.transition_to(TaskStatus.PAUSED)

        for task in (pending, running, paused):
            self.assertIs(
                task.transition_to(TaskStatus.STOPPED).status,
                TaskStatus.STOPPED,
            )

    def test_terminal_states_cannot_transition(self):
        running = Task("work").transition_to(TaskStatus.RUNNING)
        terminal = (
            running.transition_to(TaskStatus.COMPLETED),
            running.transition_to(TaskStatus.FAILED),
            running.transition_to(TaskStatus.STOPPED),
        )
        for task in terminal:
            for status in TaskStatus:
                with self.assertRaises(InvalidTaskTransitionError):
                    task.transition_to(status)

    def test_other_invalid_transitions_fail_explicitly(self):
        pending = Task("work")
        paused = pending.transition_to(TaskStatus.RUNNING).transition_to(
            TaskStatus.PAUSED
        )
        for task, status in (
            (pending, TaskStatus.COMPLETED),
            (pending, TaskStatus.PAUSED),
            (paused, TaskStatus.COMPLETED),
            (paused, TaskStatus.FAILED),
        ):
            with self.assertRaisesRegex(
                InvalidTaskTransitionError,
                f"from {task.status.value} to {status.value}",
            ):
                task.transition_to(status)
        with self.assertRaises(TypeError):
            pending.transition_to("running")  # type: ignore[arg-type]

    def test_snapshots_cannot_be_mutated(self):
        task = Task("work", goal=TaskGoal("finish"))
        with self.assertRaises(FrozenInstanceError):
            task.status = TaskStatus.RUNNING  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            task.goal = None  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            task.goal.description = "changed"  # type: ignore[misc,union-attr]
