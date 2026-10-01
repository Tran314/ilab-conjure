from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from codex_image.webui.events import event_snapshot
from tests.webui_helpers import FakeImageClient


class WebUIEventSnapshotTests(unittest.TestCase):
    def test_deleting_completed_tasks_keeps_concurrent_output_slots_in_sidebar(self) -> None:
        from codex_image.webui.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                output_root=Path(tmp),
                client_factory=lambda: FakeImageClient(),
                auth_checker=lambda: True,
                auto_start_queue=False,
            )
            now = datetime.now().astimezone().isoformat()
            active_slots = {
                "running-two": ["running", "running"],
                "running-mixed": ["completed", "running", "running"],
                "waiting-two": ["queued", "queued"],
            }
            for task_id, statuses in active_slots.items():
                app.state.storage.write_metadata(task_id, {
                    "task_id": task_id,
                    "created_at": now,
                    "updated_at": now,
                    "status": "queued" if task_id.startswith("waiting") else "running",
                    "prompt": task_id,
                    "params": {"n": len(statuses)},
                    "total_count": len(statuses),
                    "generated_count": statuses.count("completed"),
                    "failed_count": 0,
                    "input_files": [],
                    "outputs": [
                        {"index": index, "status": status}
                        for index, status in enumerate(statuses, start=1)
                    ],
                })
                if task_id.startswith("waiting"):
                    app.state.queue_storage.enqueue(task_id)
                else:
                    app.state.active_task_ids.add(task_id)
                    app.state.queue_storage.set_running(
                        f"fixture:{task_id}", task_id, auth_source="codex",
                    )
            for task_id in ("completed-delete", "completed-keep"):
                app.state.storage.write_metadata(task_id, {
                    "task_id": task_id,
                    "created_at": now,
                    "completed_at": now,
                    "updated_at": now,
                    "status": "completed",
                    "prompt": task_id,
                    "params": {"n": 1},
                    "input_files": [],
                })

            client = TestClient(app)
            self.assertEqual(client.delete("/api/tasks/completed-delete").status_code, 200)
            response = client.get("/api/tasks/sidebar?limit=50")
            self.assertEqual(response.status_code, 200)
            tasks = {task["task_id"]: task for task in response.json()["tasks"]}
            self.assertNotIn("completed-delete", tasks)
            self.assertTrue(tasks["completed-keep"]["summary_only"])
            for task_id, statuses in active_slots.items():
                self.assertEqual(
                    [output["status"] for output in tasks[task_id].get("outputs", [])],
                    statuses,
                )
                self.assertFalse(tasks[task_id].get("summary_only", False))

            # A subsequent refresh must replace active details with the new terminal state.
            task = app.state.storage.read_metadata("running-two")
            task["status"] = "completed"
            task["generated_count"] = 2
            task["completed_at"] = now
            app.state.storage.write_metadata("running-two", task)
            app.state.active_task_ids.remove("running-two")
            app.state.queue_storage.clear_running("fixture:running-two")
            tasks = {task["task_id"]: task for task in client.get("/api/tasks/sidebar").json()["tasks"]}
            self.assertEqual(tasks["running-two"]["status"], "completed")
            self.assertEqual(tasks["running-two"]["generated_count"], 2)
            self.assertTrue(tasks["running-two"]["summary_only"])

    def test_generation_snapshot_bounds_each_activity_group_and_keeps_older_active_task(self) -> None:
        from codex_image.webui.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                output_root=Path(tmp),
                client_factory=lambda: FakeImageClient(),
                auth_checker=lambda: True,
                auto_start_queue=False,
            )
            now = datetime.now().astimezone()
            active_task_id = "20260601000000-active"
            app.state.storage.write_metadata(
                active_task_id,
                {
                    "task_id": active_task_id,
                    "created_at": "2026-06-01T00:00:00+00:00",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                    "status": "queued",
                    "mode": "generate",
                    "prompt": "older active task",
                    "params": {},
                    "input_files": [],
                },
            )
            app.state.queue_storage.enqueue(active_task_id)

            for index in range(55):
                task_id = f"20260701{index:06d}-recent"
                completed_at = now.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(seconds=index)
                app.state.storage.write_metadata(
                    task_id,
                    {
                        "task_id": task_id,
                        "created_at": (now - timedelta(days=20, seconds=index)).isoformat(),
                        "updated_at": completed_at.isoformat(),
                        "completed_at": completed_at.isoformat(),
                        "status": "completed",
                        "mode": "generate",
                        "prompt": f"recent task {index}",
                        "params": {},
                        "input_files": [],
                    },
                )
            for label, days_ago in (("yesterday", 1), ("last7", 3)):
                terminal_at = now - timedelta(days=days_ago)
                app.state.storage.write_metadata(
                    label,
                    {
                        "task_id": label,
                        "created_at": now.isoformat(),
                        "updated_at": terminal_at.isoformat(),
                        "completed_at": terminal_at.isoformat(),
                        "status": "completed",
                        "mode": "generate",
                        "prompt": label,
                        "params": {},
                        "input_files": [],
                    },
                )

            snapshot = event_snapshot(app.state.ctx)

        task_ids = [task["task_id"] for task in snapshot["tasks"]]
        groups = {group["key"]: group for group in snapshot["task_groups"]}
        self.assertEqual(len(task_ids), 53)
        self.assertEqual(len(set(task_ids)), 53)
        self.assertIn(active_task_id, task_ids)
        self.assertEqual(groups["today"]["count"], 55)
        self.assertEqual(len(groups["today"]["tasks"]), 50)
        self.assertEqual(groups["yesterday"]["count"], 1)
        self.assertEqual(groups["last7"]["count"], 1)
        self.assertEqual(snapshot["queue"]["summary"]["waiting_count"], 1)


if __name__ == "__main__":
    unittest.main()
