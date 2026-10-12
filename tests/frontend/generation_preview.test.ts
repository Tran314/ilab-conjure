import assert from "node:assert/strict";
import test from "node:test";
import { GenerationPreviewState, submissionSnapshotKey } from "../../codex_image/webui/frontend/src/generation-preview-state";

class Element {
  textContent = ""; title = ""; open = false; dataset: Record<string, string> = {}; children: Element[] = [];
  replaceChildren(...items: Element[]) { this.children = items; }
  append(item: Element) { this.children.push(item); }
  addEventListener() {}
}
const elements = new Map(["executionSummary", "executionSummaryValues", "executionSummaryDetails"].map(id => [id, new Element()]));
const state: any = { generationCatalog: { models: [] }, selectedModelId: "test-model", selectedProviderId: "provider", generationPresetApplying: false, generationPresetPending: false };
let input = "first prompt";
let provider = "provider";
let count = 1;
let references = [new Blob(["original pixels"])];
const statuses: string[] = [];
function build() {
  const form = new FormData(); form.append("prompt", input); form.append("provider", provider); form.append("count", String(count));
  references.forEach(file => form.append("reference_images", file, "reference.png"));
  return { endpoint: "/api/generate", form, inputFiles: references };
}
const methods = { buildSubmissionRequest: build, setStatus: (message: string) => statuses.push(message) };
(globalThis as any).window = { __codexImageWebUI: { state, methods, els: {} } };
(globalThis as any).document = { getElementById: (id: string) => elements.get(id) || null, createElement: () => new Element(), addEventListener() {} };
const feature = await import("../../codex_image/webui/frontend/src/execution-summary");
const result = (token: string, providerName = "Provider A") => ({ preview_token: token, summary: { model: { id: "test-model", name: "Model" }, provider: { id: "provider", name: providerName }, operation: "generate", binding: { id: "binding", remote_model_id: "remote-model", protocol_profile: "profile" }, output_count: 1, task_count: 1, request_count: 1, dimensions: { size: "1024x1024" }, quality: "high", effective_parameters: { "output.count": 1 }, queue: { provider_concurrency: 2, task_request_concurrency: 1, max_provider_concurrency: 32, auto_start: false }, references: { images: [{ position: 1, kind: "upload", source_index: 0 }], files: [], ordering: "explicit_first_occurrence", rules: { images: "preserve", files: "preserve" }, limits: { max_images: 16, supports_mask: true, supports_reference_files: false } } } });
function response(token: string, providerName?: string) { return new Response(JSON.stringify(result(token, providerName))); }

test("preview revisions reject stale responses even if the user later returns to the same values", () => {
  const preview = new GenerationPreviewState();
  const first = preview.begin("A"); preview.begin("B"); const latest = preview.begin("A");
  assert.equal(preview.accept(first, "A", result("old")), false);
  assert.equal(preview.accept(latest, "A", result("latest")), true);
  assert.equal(preview.current("B"), null);
  assert.equal(preview.current("A")?.preview_token, "latest");
});

test("snapshot identity detects same-name same-size replacement image content by immutable object identity", () => {
  const first = build();
  assert.equal(submissionSnapshotKey(first), submissionSnapshotKey(build()));
  references = [new Blob(["different bytes"])];
  assert.notEqual(submissionSnapshotKey(first), submissionSnapshotKey(build()));
});

test("first submission click obtains free preview but requires another reviewed click", async () => {
  feature.invalidateExecutionSummary();
  const calls: string[] = [];
  globalThis.fetch = (async (url: string, options: RequestInit) => {
    calls.push(url); assert.equal(options.method, "POST");
    assert.equal((options.body as FormData).get("prompt"), input);
    return response("reviewed");
  }) as any;
  assert.equal(await feature.ensureExecutionPreview(build()), null);
  assert.deepEqual(calls, ["/api/generate/preview"]);
  assert.equal(elements.get("executionSummary")!.dataset.previewState, "ready");
  assert.equal(elements.get("executionSummaryDetails")!.open, true);
  assert.equal(await feature.ensureExecutionPreview(build()), "reviewed");
  assert.equal(calls.length, 1);
  const rendered = elements.get("executionSummaryValues")!.children.map(item => item.textContent).join("\n");
  assert.match(rendered, /1024x1024/); assert.match(rendered, /high/); assert.match(rendered, /#1/);
  assert.ok(!rendered.includes(input)); assert.ok(!rendered.includes("different bytes"));
});

test("parameter and provider changes immediately remove the old summary before any response", async () => {
  count = 2; provider = "provider-b";
  feature.updateExecutionSummary();
  assert.equal(elements.get("executionSummary")!.dataset.previewState, "stale");
  assert.equal(elements.get("executionSummaryValues")!.children.length, 0);
  let resolve: (response: Response) => void = () => {};
  globalThis.fetch = (() => new Promise(done => { resolve = done; })) as any;
  const review = feature.ensureExecutionPreview(build());
  assert.equal(elements.get("executionSummary")!.dataset.previewState, "loading");
  input = "newer prompt";
  feature.updateExecutionSummary();
  resolve(response("stale", "Old provider"));
  await review;
  assert.equal(elements.get("executionSummaryValues")!.children.length, 0);
  assert.notEqual(elements.get("executionSummary")!.dataset.previewState, "ready");
  feature.invalidateExecutionSummary();
});

test("an old response cannot overwrite a newer preview that completed first", async () => {
  feature.invalidateExecutionSummary();
  const resolves: Array<(response: Response) => void> = [];
  globalThis.fetch = (() => new Promise(resolve => resolves.push(resolve))) as any;
  const old = feature.ensureExecutionPreview(build());
  provider = "new-provider";
  feature.updateExecutionSummary();
  const latest = feature.ensureExecutionPreview(build());
  resolves[1]!(response("new-token", "Newest provider")); await latest;
  resolves[0]!(response("old-token", "Old provider")); await old;
  assert.equal(await feature.ensureExecutionPreview(build()), "new-token");
  assert.match(elements.get("executionSummary")!.textContent, /Newest provider/);
});

test("preview failures are actionable and never authorize a paid submission", async () => {
  feature.invalidateExecutionSummary();
  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: { code: "incompatible", message: "Reference images are forbidden" } }), { status: 422 })) as any;
  assert.equal(await feature.ensureExecutionPreview(build()), null);
  assert.equal(elements.get("executionSummary")!.dataset.previewState, "error");
  assert.match(elements.get("executionSummary")!.textContent, /Reference images are forbidden/);
  feature.invalidateExecutionSummary();
});
