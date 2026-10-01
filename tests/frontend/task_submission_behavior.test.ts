import assert from "node:assert/strict";
import test from "node:test";

class FakeClassList {
  private readonly values = new Set<string>();

  add(...names: string[]): void {
    names.forEach((name) => this.values.add(name));
  }

  remove(...names: string[]): void {
    names.forEach((name) => this.values.delete(name));
  }

  contains(name: string): boolean {
    return this.values.has(name);
  }
}

const runButton = {
  classList: new FakeClassList(),
  disabled: false,
  textContent: "开始生成",
  title: "开始生成（Cmd+Enter）",
};

const state: any = {
  authAvailable: true,
  generationCatalog: {
    schema_version: 1,
    manifest_version: 1,
    families: [{ id: "gpt-image", display_name: "GPT Image", short_name: "GPT", label_key: "family.gpt" }],
    models: [{
      id: "gpt-image-2",
      family_id: "gpt-image",
      display_name: "GPT Image 2",
      official_model_id: "gpt-image-2",
      version: 1,
      operations: ["generate"],
      parameters: [],
      input_constraints: { max_images: 1, supports_mask: false, supports_reference_files: false },
    }],
    providers: [{
      id: "provider-a",
      name: "Provider A",
      builtin: false,
      available: true,
      bindings: [{
        id: "binding-a",
        canonical_model_id: "gpt-image-2",
        remote_model_id: "gpt-image-2",
        protocol_profile: "openai_images",
        parameter_codec: "gpt_openai_images",
        operations: ["generate"],
      }],
    }],
    default_provider_by_model: { "gpt-image-2": "provider-a" },
    codex: { available: false, mode: "images" },
  },
  historyTaskReveal: null,
  historyTaskRevealSeq: 0,
  images: [],
  mode: "generate",
  parameterDraftsByModel: { "gpt-image-2": {} },
  pendingTaskId: null,
  referenceFiles: [],
  runFeedbackAction: null,
  runStartedAt: null,
  runTimerId: null,
  selectedModelId: "gpt-image-2",
  selectedProviderBindingId: "binding-a",
  selectedProviderId: "provider-a",
  selectedTaskId: null,
  tasks: [],
};

const els: any = {
  requestJson: { textContent: "" },
  runButton,
  size: { value: "1024x1024" },
};

const methods: Record<string, (...args: any[]) => any> = {};
const bridge: any = { state, els, methods };
const fakeWindow: any = {
  __codexImageWebUI: bridge,
  clearInterval() {},
  clearTimeout: globalThis.clearTimeout.bind(globalThis),
  refreshQueue: async () => {},
  setInterval: () => 1,
  setTimeout: globalThis.setTimeout.bind(globalThis),
};

(globalThis as any).window = fakeWindow;
(globalThis as any).document = { hidden: false, getElementById: () => null };

const runtimeFeedback = await import("../../codex_image/webui/frontend/src/runtime-feedback");
const { initTaskSubmitFeature } = await import("../../codex_image/webui/frontend/src/task-submit");

function resetSubmissionState(): void {
  state.historyTaskReveal = null;
  state.historyTaskRevealSeq = 0;
  state.images = [];
  state.mode = "generate";
  state.pendingTaskId = null;
  state.referenceFiles = [];
  state.runFeedbackAction = null;
  state.runStartedAt = null;
  state.runTimerId = null;
  state.selectedTaskId = null;
  state.tasks = [];
  runButton.disabled = false;
  runButton.textContent = "开始生成";
  runButton.title = "开始生成（Cmd+Enter）";
  runButton.classList.remove("running");

  Object.assign(methods, {
    addPendingTask(task: any) {
      state.pendingTaskId = task.task_id;
      state.selectedTaskId = task.task_id;
      state.tasks = [task];
    },
    currentMainModel: () => "gpt-5",
    currentPromptFidelity: () => "strict",
    currentPromptForModel: () => "测试提示词",
    currentTaskParams: () => ({ api_mode: "images", model: "gpt-image-2", n: 1, size: "1024x1024" }),
    customSizeValidationMessage: () => "",
    galleryInputs: () => [],
    getPromptText: () => "测试提示词",
    markPendingTaskFailed() {},
    missingGalleryInputs: () => [],
    missingReferenceAssetInputs: () => [],
    missingReferenceFileInputs: () => [],
    referenceAssetInputs: () => [],
    referenceFileUploads: () => [],
    refreshRecentAssets: async () => {},
    renderPreview() {},
    replacePendingTask(_pendingTaskId: string, task: any) {
      state.pendingTaskId = null;
      state.selectedTaskId = task.task_id;
      state.tasks = [task];
    },
    setStatus() {},
    sourcePreviewUrl: () => "",
    startRunFeedback() {},
    stopRunFeedback() {},
    storedReferenceFileInputs: () => [],
    syncGalleryInputsFromPrompt() {},
    syncPromptFromEditor() {},
    uploadInputs: () => [],
  });
  initTaskSubmitFeature();
}

function deferredFetch() {
  const resolvers: Array<(response: Response) => void> = [];
  let calls = 0;
  (globalThis as any).fetch = () => {
    calls += 1;
    return new Promise<Response>((resolve) => resolvers.push(resolve));
  };
  return {
    calls: () => calls,
    resolveAll() {
      resolvers.forEach((resolve, index) => resolve(new Response(JSON.stringify({
        request: {},
        task: { task_id: `queued-${index + 1}`, status: "queued" },
      }), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      })));
    },
  };
}

test("run feedback leaves the generate button visually unchanged", () => {
  resetSubmissionState();
  Object.assign(methods, {
    elapsedMillisecondsSince: () => 0,
    formatDurationTenths: () => "0.0 秒",
    renderTasks() {},
    setStatus() {},
    syncRunButtonLabel() {},
    timestampMs: (value: string) => Date.parse(value),
  });
  const before = {
    disabled: runButton.disabled,
    running: runButton.classList.contains("running"),
    textContent: runButton.textContent,
    title: runButton.title,
  };

  runtimeFeedback.startRunFeedback({
    task_id: "pending-visual",
    status: "submitting",
    created_at: new Date().toISOString(),
  } as any, "提交中");

  assert.deepEqual({
    disabled: runButton.disabled,
    running: runButton.classList.contains("running"),
    textContent: runButton.textContent,
    title: runButton.title,
  }, before);
  runtimeFeedback.stopRunFeedback();
});

test("submission keeps the generate button availability unchanged while awaiting the server", async () => {
  resetSubmissionState();
  const pendingFetch = deferredFetch();
  const submission = methods.runTask();
  let assertionError: unknown = null;
  try {
    assert.equal(pendingFetch.calls(), 1);
    assert.equal(runButton.disabled, false);
  } catch (error) {
    assertionError = error;
  } finally {
    pendingFetch.resolveAll();
    await submission;
  }
  if (assertionError) throw assertionError;
});

test("a second submission is ignored until the first server response arrives", async () => {
  resetSubmissionState();
  const pendingFetch = deferredFetch();
  const firstSubmission = methods.runTask();
  const secondSubmission = methods.runTask();
  let assertionError: unknown = null;
  try {
    assert.equal(pendingFetch.calls(), 1);
  } catch (error) {
    assertionError = error;
  } finally {
    pendingFetch.resolveAll();
    await Promise.all([firstSubmission, secondSubmission]);
  }
  if (assertionError) throw assertionError;
});

test("editing during an in-flight submission remains an unsaved draft", async () => {
  resetSubmissionState();
  const { composerHasChanges, markComposerBaseline } = await import("../../codex_image/webui/frontend/src/composer-draft");
  let prompt = "saved baseline";
  methods.getPromptText = () => prompt;
  methods.currentPromptForModel = () => prompt;
  markComposerBaseline();
  prompt = "first request";
  const pendingFetch = deferredFetch();
  const submission = methods.runTask();
  prompt = "second draft typed while waiting";
  pendingFetch.resolveAll();
  await submission;
  assert.equal(composerHasChanges(), true);
});

test("accepted submissions refresh recent uploads only when their image inputs change the list", async () => {
  const upload = { name: "fixture.png", file: new Blob(["fixture"], { type: "image/png" }) };
  const cases = [
    { name: "text only", mode: "generate", uploads: [], assets: [], galleries: [], files: [], refreshes: 0 },
    { name: "uploaded image", mode: "generate", uploads: [upload], assets: [], galleries: [], files: [], refreshes: 1 },
    { name: "recent image", mode: "generate", uploads: [], assets: [{ id: "recent-image" }], galleries: [], files: [], refreshes: 1 },
    { name: "gallery image", mode: "generate", uploads: [], assets: [], galleries: [{ id: "gallery-image" }], files: [], refreshes: 0 },
    { name: "reference file", mode: "generate", uploads: [], assets: [], galleries: [], files: [{ filename: "fixture.txt", file: new Blob(["fixture"]) }], refreshes: 0 },
    { name: "edit uploaded image", mode: "edit", uploads: [upload], assets: [], galleries: [], files: [], refreshes: 1 },
    { name: "edit recent image", mode: "edit", uploads: [], assets: [{ id: "recent-image" }], galleries: [], files: [], refreshes: 1 },
    { name: "edit gallery image", mode: "edit", uploads: [], assets: [], galleries: [{ id: "gallery-image" }], files: [], refreshes: 0 },
  ];
  for (const scenario of cases) {
    resetSubmissionState();
    state.mode = scenario.mode;
    let refreshes = 0;
    Object.assign(methods, {
      uploadInputs: () => scenario.uploads,
      referenceAssetInputs: () => scenario.assets,
      galleryInputs: () => scenario.galleries,
      referenceFileUploads: () => scenario.files,
      refreshRecentAssets: async () => { refreshes += 1; },
    });
    const pendingFetch = deferredFetch();
    const submission = methods.runTask();
    pendingFetch.resolveAll();
    await submission;
    assert.equal(state.tasks[0].status, "queued", scenario.name);
    assert.equal(refreshes, scenario.refreshes, scenario.name);
  }
});

test("recent upload refresh follows submitted inputs rather than in-flight draft edits", async () => {
  for (const submittedWithImage of [false, true]) {
    resetSubmissionState();
    let assets = submittedWithImage ? [{ id: "recent-image" }] : [];
    let refreshes = 0;
    methods.referenceAssetInputs = () => assets;
    methods.refreshRecentAssets = async () => { refreshes += 1; };
    const pendingFetch = deferredFetch();
    const submission = methods.runTask();
    assets = submittedWithImage ? [] : [{ id: "new-draft-image" }];
    pendingFetch.resolveAll();
    await submission;
    assert.equal(refreshes, Number(submittedWithImage));
  }
});

test("submission preview uses the latest task after queue and asset requests", async () => {
  resetSubmissionState();
  const statuses: string[] = [];
  methods.renderPreview = (task?: any) => statuses.push((task || state.tasks[0]).status);
  fakeWindow.refreshQueue = async () => {
    state.tasks = [{ ...state.tasks[0], status: "failed", error: "HTTP 404 model_not_found" }];
  };
  const pendingFetch = deferredFetch();
  const submission = methods.runTask();
  pendingFetch.resolveAll();
  await submission;
  fakeWindow.refreshQueue = async () => {};
  assert.equal(statuses.at(-1), "failed");
});

test("a late submit or viewed response cannot replace newer task progress", () => {
  resetSubmissionState();
  const completed: any = { task_id: "race", status: "failed", updated_at: "2026-09-14T06:15:42Z" };
  const queued: any = { task_id: "race", status: "queued", updated_at: "2026-09-14T06:15:40Z" };
  state.tasks = [{ task_id: "pending", status: "submitting", local_pending: true }, completed];
  Object.assign(methods, { renderTasks() {}, renderPreview() {}, revokeTaskUploadPreviewUrls() {} });
  runtimeFeedback.replacePendingTask("pending", queued);
  assert.equal(state.tasks.length, 1);
  assert.equal(state.tasks[0].status, "failed");
  assert.equal(runtimeFeedback.updateTaskInState(queued), false);
  assert.equal(state.tasks[0].status, "failed");
  const retry: any = { ...queued, updated_at: "2026-09-14T06:16:00Z" };
  assert.equal(runtimeFeedback.updateTaskInState(retry), true);
  assert.equal(state.tasks[0].status, "queued", "a new retry must still be accepted");
});

test("accepted submissions retire matching drafts but protect unrelated and in-flight edits", async () => {
  resetSubmissionState();
  let prompt = "";
  let beforeUnload: (event: any) => void = () => {};
  const restoreButton = { hidden: true, textContent: "", addEventListener() {} };
  (globalThis as any).document.getElementById = () => restoreButton;
  fakeWindow.addEventListener = (name: string, handler: any) => {
    if (name === "beforeunload") beforeUnload = handler;
  };
  methods.getPromptText = () => prompt;
  methods.setPromptText = (value: string) => { prompt = value; };
  methods.currentPromptForModel = () => prompt;
  methods.setMode = (value: string) => { state.mode = value; };
  const drafts = await import("../../codex_image/webui/frontend/src/composer-draft");
  drafts.initComposerDraft();
  const unloadBlocked = () => {
    let blocked = false;
    beforeUnload({ preventDefault: () => { blocked = true; } });
    return blocked;
  };

  prompt = "submitted draft";
  drafts.preserveComposerDraft();
  assert.equal(restoreButton.hidden, false);
  let pendingFetch = deferredFetch();
  let submission = methods.runTask();
  pendingFetch.resolveAll();
  await submission;
  assert.equal(restoreButton.hidden, true);
  assert.equal(unloadBlocked(), false);

  prompt = "unrelated draft";
  drafts.preserveComposerDraft();
  prompt = "second submission";
  drafts.preserveComposerDraft();
  pendingFetch = deferredFetch();
  submission = methods.runTask();
  prompt = "new edits while submitting";
  pendingFetch.resolveAll();
  await submission;
  assert.equal(drafts.composerHasChanges(), true);
  assert.equal(unloadBlocked(), true);
  drafts.restoreComposerDraft();
  assert.equal(prompt, "unrelated draft", "only the accepted submission was retired");
});
