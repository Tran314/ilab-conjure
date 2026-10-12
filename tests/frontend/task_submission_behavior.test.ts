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
const stored = new Map<string, string>();
const fakeWindow: any = {
  localStorage: { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => stored.set(key, value), removeItem: (key: string) => stored.delete(key) },
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
const galleryChips = await import("../../codex_image/webui/frontend/src/prompt-gallery-chips");
const promptModel = await import("../../codex_image/webui/frontend/src/prompt-model");

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
    categoryPromptRole: () => "test role",
    currentPromptFidelity: () => "strict",
    currentPromptForModel: () => "测试提示词",
    currentTaskParams: () => ({ api_mode: "images", model: "gpt-image-2", n: 1, size: "1024x1024" }),
    customSizeValidationMessage: () => "",
    // This suite isolates accepted-submission behavior; review races have their own suite.
    ensureExecutionPreview: () => "fixture-reviewed-token",
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
    async resolveAll() {
      // File content hashing is asynchronous; wait until submission reaches fetch.
      for (let index = 0; index < 50 && !resolvers.length; index++) await new Promise(resolve => setTimeout(resolve, 0));
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
    await pendingFetch.resolveAll();
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
    await pendingFetch.resolveAll();
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
  await pendingFetch.resolveAll();
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
    await pendingFetch.resolveAll();
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
    await pendingFetch.resolveAll();
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
  await pendingFetch.resolveAll();
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
  await pendingFetch.resolveAll();
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
  await pendingFetch.resolveAll();
  await submission;
  assert.equal(drafts.composerHasChanges(), true);
  assert.equal(unloadBlocked(), true);
  drafts.restoreComposerDraft();
  assert.equal(prompt, "unrelated draft", "only the accepted submission was retired");
});

function referenceFixture() {
  resetSubmissionState();
  let chips: any[] = [];
  els.promptEditor = { querySelectorAll: () => chips };
  Object.assign(methods, {
    galleryInputs: () => state.images.filter((source: any) => source.kind === "gallery"),
    referenceAssetInputs: () => state.images.filter((source: any) => source.kind === "asset"),
    uploadInputs: () => state.images.filter((source: any) => source.kind === "upload"),
    renderImageStrip() {}, updateRequestPreview() {},
    setMode: (mode: string) => { state.mode = mode; },
    gallerySource: (item: any) => ({ kind: "gallery", ...item }),
    findGalleryItem: (id: string) => ({ id, name: id, image_url: `/gallery/${id}` }),
    syncGalleryInputsFromPrompt: galleryChips.syncGalleryInputsFromPrompt,
  });
  const gallery = (id: string) => ({ kind: "gallery", id, name: id });
  const asset = (id: string) => ({ kind: "asset", id, name: id });
  const upload = (name: string) => ({ kind: "upload", name, file: new File([name], name) });
  return { gallery, asset, upload, mention(ids: string[]) { chips = ids.map(id => ({ dataset: { galleryId: id } })); } };
}

test("gallery prompt sync keeps interleaved inputs and appends only new unique mentions", () => {
  const f = referenceFixture();
  const a = f.gallery("A"), b = f.gallery("B"), c = f.upload("C"), d = f.asset("D");
  state.images = [a, d, b, c];
  f.mention(["B", "A", "B"]);
  assert.equal(galleryChips.syncGalleryInputsFromPrompt(), false);
  assert.deepEqual(state.images, [a, d, b, c]);
  f.mention(["B", "E", "E"]);
  galleryChips.syncGalleryInputsFromPrompt();
  assert.deepEqual(state.images.map((source: any) => source.name), ["D", "B", "C", "E"]);
  delete els.promptEditor;
});

test("gallery instructions defer numbering until the server deduplicates references", () => {
  const f = referenceFixture();
  const a = f.gallery("A"), b = f.gallery("B");
  state.images = [f.upload("D.png"), f.upload("D.png"), a, f.asset("C"), b];
  methods.categoryPromptRole = () => "test role";
  methods.expandPromptSnippets = (prompt: string) => `${prompt} expanded`;
  const context = promptModel.galleryPrompt();
  assert.ok(context);
  assert.deepEqual(context.references.map((ref: any) => ref.id), ["A", "B"]);
  assert.match(context.template, /\{number\}/);
  assert.equal(promptModel.currentPromptForModel(), "测试提示词 expanded");
  delete els.promptEditor;
});

for (const mode of ["generate", "edit"]) {
  for (const fidelity of ["off", "strict", "original"]) {
    test(`${mode} submits localized gallery context only in ${fidelity} mode`, async () => {
      const f = referenceFixture();
      const d = { ...f.gallery("D"), prompt_note: "literal {number}" };
      state.images = [f.upload("C.png"), f.upload("C.png"), d, f.asset("A"), f.gallery("E")];
      f.mention(["D", "E"]);
      state.mode = mode;
      els.promptFidelity = { value: fidelity };
      methods.categoryPromptRole = () => "role {name}";
      methods.expandPromptSnippets = (prompt: string) => `${prompt} expanded`;
      promptModel.initPromptModelFeature();
      let form: FormData | undefined;
      (globalThis as any).fetch = async (_url: string, init: any) => {
        form = init.body;
        return new Response(JSON.stringify({ task: { task_id: "numbered", status: "queued" } }));
      };
      const preview = methods.buildPreviewRequest();
      await methods.runTask();
      assert.ok(form);
      assert.equal(form.get("prompt_for_model"), "测试提示词 expanded");
      if (fidelity === "original") {
        assert.equal(form.has("gallery_prompt"), false);
        assert.equal(preview.gallery_prompt, undefined);
      } else {
        const context = JSON.parse(String(form.get("gallery_prompt")));
        assert.deepEqual(context, preview.gallery_prompt);
        assert.deepEqual(context.references.map((ref: any) => ref.id), ["D", "E"]);
        assert.equal(context.references[0].note, " literal {number}");
        assert.equal(context.references[0].role, "role {name}");
        assert.match(context.template, /\{number\}/);
      }
      delete els.promptFidelity;
      delete els.promptEditor;
    });
  }
}

for (const mode of ["generate", "edit"]) {
  for (const replacement of ["new upload", "edited upload"]) {
    test(`${mode} submits reference bar order after last image becomes ${replacement}`, async () => {
      const f = referenceFixture();
      const a = f.gallery("A"), b = f.asset("B"), c = f.gallery("C");
      state.images = [a, b, f.upload("middle.png"), c];
      // Both removal + append and saving the editor's replacement preserve this slot.
      if (replacement === "new upload") state.images.pop();
      state.images[3] = f.upload("replacement.png");
      f.mention(["A"]);
      state.mode = mode;
      let form: FormData | undefined;
      (globalThis as any).fetch = async (url: string, init: any) => {
        assert.equal(url, `/api/${mode}`);
        form = init.body;
        return new Response(JSON.stringify({task: {task_id: "ordered", status: "queued"}}));
      };
      await methods.runTask();
      assert.deepEqual(state.images.map((source: any) => source.name), ["A", "B", "middle.png", "replacement.png"]);
      assert.ok(form);
      assert.deepEqual(JSON.parse(String(form.get("reference_image_order"))), [
        {kind:"gallery", id:"A"}, {kind:"asset", id:"B"},
        {kind:"upload", index:0}, {kind:"upload", index:1},
      ]);
      const field = mode === "edit" ? "images" : "reference_images";
      assert.deepEqual(form.getAll(field).map((file: any) => file.name), ["middle.png", "replacement.png"]);
      delete els.promptEditor;
    });
  }
}

test("a lost submission response keeps its identity and unknown result until identical retry succeeds", async () => {
  resetSubmissionState();
  stored.clear();
  const identities: string[] = [];
  let fail = true;
  globalThis.fetch = (async (_url: string, options: any) => {
    identities.push(String(options.body.get("client_request_id")));
    if (fail) throw new TypeError("Network connection lost");
    return new Response(JSON.stringify({request:{},task:{task_id:"recovered-request",status:"queued"}}),{status:200,headers:{"Content-Type":"application/json"}});
  }) as any;
  await methods.runTask();
  assert.equal(state.tasks[0].submission_unknown, true);
  assert.equal(state.tasks[0].result_certainty, "submitted_unknown");
  fail=false;
  await methods.runTask();
  assert.equal(identities.length,2);
  assert.equal(identities[0],identities[1]);
  assert.equal(state.tasks[0].task_id,"recovered-request");
  await methods.runTask();
  assert.notEqual(identities[2],identities[1], "an acknowledged successful submission permits intentional new work");
});

test("submission is gated by a reviewed preview and carries its token without changing the request identity body", async () => {
  resetSubmissionState();
  (globalThis as any).document.getElementById = () => null;
  stored.clear();
  const summary = await import("../../codex_image/webui/frontend/src/execution-summary");
  summary.invalidateExecutionSummary();
  methods.ensureExecutionPreview = summary.ensureExecutionPreview;
  const calls: Array<{ url: string; options: any }> = [];
  globalThis.fetch = (async (url: string, options: any) => {
    calls.push({ url, options });
    return new Response(JSON.stringify(url.endsWith("/preview")
      ? { preview_token: "reviewed-token", summary: { output_count: 1, model: { id: "gpt-image-2" }, provider: { id: "provider-a" } } }
      : { task: { task_id: "reviewed-task", status: "queued" } }));
  }) as any;
  await methods.runTask();
  assert.deepEqual(calls.map(call => call.url), ["/api/generate/preview"]);
  assert.equal(state.tasks.length, 0);
  await methods.runTask();
  assert.deepEqual(calls.map(call => call.url), ["/api/generate/preview", "/api/generate"]);
  assert.equal(calls[1]!.options.headers["X-Generation-Preview-Token"], "reviewed-token");
  assert.ok(calls[1]!.options.body.get("client_request_id"));
  assert.equal(calls[1]!.options.body.has("preview_token"), false);
});

test("a refreshed preview after a lost submission response keeps the same retry identity", async () => {
  resetSubmissionState();
  (globalThis as any).document.getElementById = () => null;
  stored.clear();
  const summary = await import("../../codex_image/webui/frontend/src/execution-summary");
  summary.invalidateExecutionSummary();
  methods.ensureExecutionPreview = summary.ensureExecutionPreview;
  let previews = 0;
  const identities: string[] = [];
  globalThis.fetch = (async (url: string, options: any) => {
    if (url.endsWith("/preview")) {
      previews += 1;
      return new Response(JSON.stringify({ preview_token: `review-${previews}`, summary: { output_count: 1 } }));
    }
    identities.push(options.body.get("client_request_id"));
    if (identities.length === 1) throw new TypeError("Response was lost after acceptance");
    return new Response(JSON.stringify({ task: { task_id: "recovered-safe-retry", status: "queued" } }));
  }) as any;
  await methods.runTask();
  await methods.runTask();
  assert.equal(state.tasks[0].submission_unknown, true);
  summary.invalidateExecutionSummary();
  await methods.runTask();
  await methods.runTask();
  assert.equal(previews, 2);
  assert.equal(identities.length, 2);
  assert.equal(identities[0], identities[1]);
  assert.equal(state.tasks[0].task_id, "recovered-safe-retry");
});

test("double clicks share the review gate and changing inputs while reviewing never submits", async () => {
  resetSubmissionState();
  (globalThis as any).document.getElementById = () => null;
  const summary = await import("../../codex_image/webui/frontend/src/execution-summary");
  summary.invalidateExecutionSummary();
  methods.ensureExecutionPreview = summary.ensureExecutionPreview;
  let prompt = "original";
  methods.getPromptText = () => prompt;
  methods.currentPromptForModel = () => prompt;
  let calls = 0;
  let resolve: (response: Response) => void = () => {};
  globalThis.fetch = ((url: string) => {
    assert.equal(url, "/api/generate/preview");
    calls += 1;
    return new Promise(done => { resolve = done; });
  }) as any;
  const first = methods.runTask();
  const second = methods.runTask();
  assert.equal(calls, 1);
  prompt = "new draft during review";
  resolve(new Response(JSON.stringify({ preview_token: "outdated", summary: { output_count: 1 } })));
  await Promise.all([first, second]);
  assert.equal(state.tasks.length, 0);
  assert.equal(calls, 1);
  summary.invalidateExecutionSummary();
});

test("a rejected reference-file submission marks the pending task failed and renders that failure", async () => {
  resetSubmissionState();
  (globalThis as any).document.getElementById = () => null;
  const previews: any[] = [];
  const referenceFile = { filename: "reference.txt", file: new Blob(["fixture reference"], { type: "text/plain" }) };
  methods.referenceFileUploads = () => [referenceFile];
  methods.markPendingTaskFailed = runtimeFeedback.markPendingTaskFailed;
  methods.renderTasks = () => {};
  methods.renderPreview = (task: any) => previews.push({ ...task });
  globalThis.fetch = (async (url: string) => {
    assert.equal(url, "/api/generate");
    return new Response(JSON.stringify({ detail: { code: "reference_files_require_responses" } }), { status: 400 });
  }) as any;
  await methods.runTask();
  assert.equal(state.tasks[0].status, "failed");
  assert.equal(state.pendingTaskId, null);
  assert.equal(previews.length, 1);
  assert.equal(previews[0].task_id, state.tasks[0].task_id);
  assert.equal(previews[0].status, "failed");
  assert.match(previews[0].error, /Responses/);
  assert.notEqual(state.tasks[0].submission_unknown, true);
});

test("confirmed lost-response replay retires its old unknown placeholder using the real persistent task store", async () => {
  resetSubmissionState();
  stored.clear();
  const revoked: string[] = [];
  const rendered: string[][] = [];
  Object.assign(methods, {
    addPendingTask: runtimeFeedback.addPendingTask,
    replacePendingTask: runtimeFeedback.replacePendingTask,
    markPendingTaskFailed: runtimeFeedback.markPendingTaskFailed,
    renderTasks: () => rendered.push(state.tasks.map((task: any) => task.task_id)),
    renderPreview() {},
    revokeTaskUploadPreviewUrls: (task: any) => revoked.push(task.task_id),
  });
  const clientIds: string[] = [];
  let accepted = false;
  globalThis.fetch = (async (_url: string, options: any) => {
    clientIds.push(options.body.get("client_request_id"));
    if (!accepted) throw new TypeError("Accepted by server, but response lost");
    return new Response(JSON.stringify({ task: { task_id: "single-confirmed-task", status: "queued" } }));
  }) as any;
  await methods.runTask();
  const original = state.tasks[0];
  assert.equal(original.local_pending, true);
  assert.equal(original.submission_unknown, true);
  assert.equal(original.client_request_id, clientIds[0]);
  // Avoid identical millisecond-based placeholder IDs hiding a duplicate-card bug.
  await new Promise(resolve => setTimeout(resolve, 2));
  await methods.runTask();
  assert.equal(state.tasks.length, 2, "another lost response must not claim either earlier attempt was resolved");
  assert.ok(state.tasks.every((task: any) => task.local_pending && task.submission_unknown));
  await new Promise(resolve => setTimeout(resolve, 2));
  accepted = true;
  await methods.runTask();
  assert.equal(clientIds[0], clientIds[1]);
  assert.equal(clientIds[1], clientIds[2]);
  assert.ok(rendered.some(ids => ids.length === 2), "retry must exercise preservation of the old local card");
  assert.deepEqual(state.tasks.map((task: any) => task.task_id), ["single-confirmed-task"]);
  assert.equal(state.tasks.some((task: any) => task.local_pending), false);
  assert.equal(state.tasks.some((task: any) => task.submission_unknown), false);
  assert.ok(revoked.includes(original.task_id));
  assert.equal(revoked.length, 3, "all retired local placeholders release their preview URLs");
  assert.equal(state.selectedTaskId, "single-confirmed-task");
});

test("confirmed replay preserves unrelated, unconfirmed and real server unknown tasks and their input references", () => {
  resetSubmissionState();
  const identity = "confirmed-client-request";
  const retry = { task_id: "pending-retry", local_pending: true, status: "submitting", client_request_id: identity };
  const recovered = { task_id: "pending-old", local_pending: true, status: "failed", submission_unknown: true, client_request_id: identity };
  const unrelated = { ...recovered, task_id: "pending-other", client_request_id: "different-client-request" };
  const uncorrelated = { ...recovered, task_id: "pending-legacy", client_request_id: undefined };
  const unconfirmed = { ...retry, task_id: "pending-unconfirmed" };
  const serverUnknown = { task_id: "server-unknown", status: "failed", submission_unknown: true, client_request_id: identity };
  const latestServer = { task_id: "server-found", status: "running", updated_at: "2026-10-12T20:00:02Z" };
  const staleReplay = { ...latestServer, status: "queued", updated_at: "2026-10-12T20:00:01Z" };
  const revoked: string[] = [];
  Object.assign(methods, { renderTasks() {}, renderPreview() {}, revokeTaskUploadPreviewUrls: (task: any) => revoked.push(task.task_id) });
  state.tasks = [retry, recovered, unrelated, uncorrelated, unconfirmed, serverUnknown, latestServer];
  runtimeFeedback.replacePendingTask(retry.task_id, staleReplay as any, identity);
  assert.equal(state.tasks[0], latestServer, "replay must preserve newer server progress");
  assert.deepEqual(new Set(state.tasks.slice(1)), new Set([unrelated, uncorrelated, unconfirmed, serverUnknown]));
  assert.deepEqual(new Set(revoked), new Set([retry.task_id, recovered.task_id]));
});

test("a response without confirmed identity never retires earlier unknown placeholders", () => {
  resetSubmissionState();
  const unknown = { task_id: "pending-old", local_pending: true, status: "failed", submission_unknown: true, client_request_id: "unconfirmed-request" };
  state.tasks = [{ task_id: "pending-current", local_pending: true, status: "submitting" }, unknown];
  Object.assign(methods, { renderTasks() {}, renderPreview() {}, revokeTaskUploadPreviewUrls() {} });
  runtimeFeedback.replacePendingTask("pending-current", { task_id: "new-server-task", status: "queued" } as any);
  assert.equal(state.tasks.includes(unknown), true);
});

test("recovered placeholders release only orphaned preview URLs through the real input cleanup", async () => {
  resetSubmissionState();
  (globalThis as any).document.addEventListener = () => {};
  const { initInputSourcesFeature } = await import("../../codex_image/webui/frontend/src/input-sources");
  initInputSourcesFeature();
  const originalRevoke = URL.revokeObjectURL;
  const revoked: string[] = [];
  URL.revokeObjectURL = (url: string) => { revoked.push(url); };
  const input = (url: string) => ({ kind: "upload", previewUrl: url });
  const inputs = [input("blob:orphan"), input("blob:unresolved-reference"), input("blob:composer-reference")];
  const identity = "recovered-request";
  const recovered = { task_id: "pending-old", local_pending: true, submission_unknown: true, client_request_id: identity, local_input_files: inputs };
  const retry = { task_id: "pending-current", local_pending: true, client_request_id: identity, local_input_files: inputs };
  const unresolved = { task_id: "pending-unrelated", local_pending: true, submission_unknown: true, client_request_id: "different-request", local_input_files: [input("blob:unresolved-reference")] };
  state.images = [input("blob:composer-reference")];
  state.tasks = [retry, recovered, unresolved];
  Object.assign(methods, { renderTasks() {}, renderPreview() {} });
  try {
    runtimeFeedback.replacePendingTask(retry.task_id, { task_id: "confirmed-server-task", status: "queued" } as any, identity);
    assert.deepEqual(new Set(revoked), new Set(["blob:orphan"]));
    assert.equal(state.tasks.includes(unresolved), true);
    assert.equal(state.images[0].previewUrl, "blob:composer-reference");
  } finally { URL.revokeObjectURL = originalRevoke; }
});
