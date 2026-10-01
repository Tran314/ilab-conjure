import assert from "node:assert/strict";
import test from "node:test";

test("sidebar refresh keeps detailed output-slot states from the active queue", async () => {
  const activeTask = {
    task_id: "running-multi",
    status: "running",
    total_count: 2,
    generated_count: 0,
    failed_count: 0,
    outputs: [
      { index: 1, status: "running" },
      { index: 2, status: "running" },
    ],
  };
  const sidebarSummary = {
    task_id: "running-multi",
    summary_only: true,
    status: "running",
    total_count: 2,
    generated_count: 0,
    failed_count: 0,
  };
  const state: any = {
    tasks: [],
    queue: {
      waiting: [],
      running: [activeTask],
      summary: {
        waiting_count: 0,
        running_count: 1,
        channel_count: 2,
      },
    },
    tasksRequestSeq: 1,
    pendingTaskId: null,
    selectedTaskId: null,
  };
  const bridge: any = {
    state,
    els: {},
    constants: { defaultDocumentTitle: "iLab CONJURE" },
    boot() {},
    methods: {
      cleanupSessionSelections() {},
      renderTasks() {},
      renderArchiveButton() {},
      renderArchiveModal() {},
      renderPreview() {},
      revokeTaskUploadPreviewUrls() {},
    },
  };
  (globalThis as any).window = { __codexImageWebUI: bridge };

  const { initTaskFeature } = await import(
    "../../codex_image/webui/frontend/src/tasks"
  );
  initTaskFeature();
  await bridge.methods.applyTasksSnapshot(
    [sidebarSummary],
    { requestSeq: 1 },
  );

  assert.deepEqual(
    state.tasks[0]?.outputs?.map((record: any) => record.status),
    ["running", "running"],
  );
  assert.equal(state.tasks[0]?.summary_only, false);

  const { acceptQueueSnapshot } = await import("../../codex_image/webui/frontend/src/state-sync");
  const { initTaskDerivedFeature } = await import("../../codex_image/webui/frontend/src/task-derived");
  initTaskDerivedFeature();
  acceptQueueSnapshot(state, { instance: "fixture", revision: 10 });
  state.tasks.push({ task_id: "completed-delete", status: "completed" });
  const concurrentTasks = [
    activeTask,
    { ...activeTask, task_id: "running-three", total_count: 3, outputs: [
      { index: 1, status: "completed", url: "/outputs/fixture.png" },
      { index: 2, status: "running" },
      { index: 3, status: "running" },
    ], generated_count: 1 },
  ];
  const previousFetch = globalThis.fetch;
  (globalThis as any).fetch = async (url: string) => {
    assert.equal(url, "/api/tasks/sidebar?limit=50");
    return new Response(JSON.stringify({
      tasks: concurrentTasks,
      sync: { instance: "fixture", revision: 11 },
    }));
  };
  try {
    // Deletion advances the server revision beyond the cached queue snapshot.
    await bridge.methods.refreshTasksAfterDeletion();
  } finally {
    globalThis.fetch = previousFetch;
  }
  assert.equal(state.tasks.some((task: any) => task.task_id === "completed-delete"), false);
  assert.deepEqual(state.tasks.map(bridge.methods.taskImageBlockStates), [
    ["running", "running"],
    ["completed", "running", "running"],
  ]);

  await bridge.methods.applyTasksSnapshot([
    { ...sidebarSummary, status: "completed", generated_count: 2 },
    concurrentTasks[1],
  ], { sync: { instance: "fixture", revision: 12 } });
  assert.deepEqual(bridge.methods.taskImageBlockStates(state.tasks[0]), ["completed", "completed"]);
  await bridge.methods.applyTasksSnapshot(concurrentTasks, {
    sync: { instance: "fixture", revision: 11 },
  });
  assert.equal(state.tasks[0].status, "completed");

  state.queue = { waiting: [], running: [] };
  state.expandedTaskGroupKey = "today";
  for (const loadedCount of [80, 180]) {
    for (const refresh of [bridge.methods.refreshTasksAfterDeletion, bridge.methods.refreshTasks]) {
      const remaining = Array.from({ length: loadedCount - 1 }, (_, index) => ({
        task_id: `history-${index}`, status: "completed", summary_only: true,
      }));
      state.tasks = remaining;
      state.taskSidebarGroupLoadedCounts = { today: loadedCount };
      const requests: string[] = [];
      globalThis.fetch = async (input: any) => {
        const url = String(input);
        requests.push(url);
        if (url === "/api/tasks/sidebar?limit=50") {
          return new Response(JSON.stringify({
            tasks: remaining.slice(0, 50),
            task_groups: [{ key: "today", count: remaining.length, tasks: remaining.slice(0, 50) }],
            sync: { instance: "fixture", revision: loadedCount },
          }));
        }
        const query = new URL(url, "http://fixture.invalid");
        assert.equal(query.pathname, "/api/tasks/sidebar/groups/today");
        const offset = Number(query.searchParams.get("offset"));
        const limit = Number(query.searchParams.get("limit"));
        assert.ok(limit > 0 && limit <= 100);
        const page = remaining.slice(offset, offset + limit);
        return new Response(JSON.stringify({ tasks: page, count: remaining.length, next_offset: offset + page.length }));
      };
      try {
        await refresh();
      } finally {
        globalThis.fetch = previousFetch;
      }
      assert.equal(state.tasks.at(-1)?.task_id, remaining.at(-1)?.task_id, "deleting a paged task must retain the visible history window");
      assert.equal(state.taskSidebarGroupLoadedCounts.today, remaining.length);
      assert.equal(requests.length, 1 + Math.ceil((remaining.length - 50) / 100));
    }
  }
});
