import assert from "node:assert/strict";
import test from "node:test";

class Element {
  value = ""; textContent = ""; title = ""; hidden = false; disabled = false; open = false; focused = false;
  dataset: Record<string, string> = {};
  children: Element[] = [];
  listeners = new Map<string, Array<(event: any) => void>>();
  classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } };
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren(...children: Element[]) { this.children = children; }
  addEventListener(type: string, fn: (event: any) => void) { this.listeners.set(type, [...(this.listeners.get(type) || []), fn]); }
  fire(type: string, event: any = {}) { (this.listeners.get(type) || []).forEach(fn => fn(event)); }
  focus() { this.focused = true; }
}
const ids = ["generationPresetsPanel", "generationPresetSelect", "generationPresetName", "generationPresetStatus", "applyGenerationPreset", "saveGenerationPreset", "renameGenerationPreset", "deleteGenerationPreset", "refreshGenerationPresets", "generationPresetDeleteConfirmation", "generationPresetDeleteQuestion", "cancelGenerationPresetDelete", "confirmGenerationPresetDelete", "generationPresetImageRule", "generationPresetFileRule", "clearGenerationPresetRules"];
const elements = new Map(ids.map(id => [id, new Element()]));
const definition = (id: string, value: unknown, values: unknown[]) => ({ id, default: value, allowed_values: values, value_type: typeof value === "number" ? "integer" : "string", operations: ["generate", "edit"], visible_when: [], minimum: null, maximum: null, step: null, label_key: id });
const model = { id: "nano-banana-2", family_id: "nano-banana", version: 1, display_name: "Nano Banana 2", operations: ["generate", "edit"], parameters: [definition("canvas.aspect_ratio", "1:1", ["1:1", "3:4"]), definition("output.count", 1, [1, 2])], input_constraints: { max_images: 10, supports_mask: false, supports_reference_files: false } };
const catalog = { models: [model], providers: [{ id: "provider-a", available: true, bindings: [{ id: "binding-a", canonical_model_id: model.id, operations: ["generate", "edit"], protocol_profile: "gemini" }] }], codex: { available: false, mode: "images" } };
const state: any = { generationCatalog: catalog, images: [], referenceFiles: [], mode: "generate", selectedModelId: model.id, selectedProviderId: "provider-a", selectedProviderBindingId: "binding-a", parameterDraftsByModel: { [model.id]: {} }, lastModelByFamily: {}, lastProviderByModel: {}, lastProviderSelectionByModel: {}, parameterDraftVersionsByModel: {} };
let prompt = "Private prompt that must survive";
const methods: any = { invalidateExecutionSummary() {}, updateRequestPreview() {}, persistModelSelection() {} };
methods.buildSubmissionRequest = () => {
  const form = new FormData();
  form.append("prompt", prompt);
  form.append("selection", JSON.stringify([state.mode, state.selectedModelId, state.selectedProviderId, state.selectedProviderBindingId, state.parameterDraftsByModel, state.images.map((item: any) => item.id)]));
  return { endpoint: "/api/generate", form, inputFiles: state.images.flatMap((item: any) => item.file ? [item.file] : []) };
};
(globalThis as any).window = { __codexImageWebUI: { state, methods, els: {} }, requestAnimationFrame: (fn: any) => fn() };
(globalThis as any).document = { getElementById: (id: string) => elements.get(id) || null, createElement: () => new Element(), querySelectorAll: () => [], addEventListener() {} };
const feature = await import("../../codex_image/webui/frontend/src/generation-presets");
feature.initGenerationPresetsFeature();
const preset = (id: string, count = 1) => ({ id, name: `中文预设 ${id}`, operation: "generate", canonical_model_id: model.id, provider_id: "provider-a", binding_id: "binding-a", parameters: { "canvas.aspect_ratio": "3:4", "output.count": count }, reference_rules: { images: "preserve", files: "preserve", order: "preserve" } });
let stored: any[] = [preset("a"), preset("b", 2)];
async function load() {
  globalThis.fetch = (async () => new Response(JSON.stringify({ presets: stored }))) as any;
  await feature.refreshGenerationPresets();
}
function select(id: string) { elements.get("generationPresetSelect")!.value = id; elements.get("generationPresetSelect")!.fire("change"); }

test("preset names reject blanks, duplicates and overlong names but support 120 Chinese characters", async () => {
  await load();
  assert.throws(() => feature.validatePresetName(" \n "));
  assert.throws(() => feature.validatePresetName("中文预设 a"));
  assert.equal(feature.validatePresetName("卡".repeat(120)), "卡".repeat(120));
  assert.throws(() => feature.validatePresetName("卡".repeat(121)));
  assert.equal(feature.validatePresetName(" 全角Ａ "), "全角A");
});

test("save uses a narrow settings allowlist and does not collect prompt, keys, paths or image content", async () => {
  state.apiKey = "do-not-save";
  state.outputPath = "/private/output";
  state.images = [{ id: "image-a", file: new Blob(["private pixels"]) }];
  state.referenceFiles = [{ id: "file-a", filename: "/private/file" }];
  elements.get("generationPresetImageRule")!.value = "require";
  const payload = feature.currentPresetPayload("保存配置");
  assert.deepEqual(Object.keys(payload).sort(), ["binding_id", "canonical_model_id", "name", "operation", "parameters", "provider_id", "reference_rules", "request_options"]);
  const json = JSON.stringify(payload);
  for (const forbidden of [prompt, "do-not-save", "/private", "private pixels", "image-a", "file-a"]) assert.ok(!json.includes(forbidden));
  assert.equal(payload.reference_rules.images, "require");
});

test("consecutive asynchronous applies commit only the newest preset and preserve prompt and exact input order", async () => {
  await load();
  const images = [{ id: "gallery-1" }, { id: "upload-2", file: new Blob(["original"]) }];
  const files = [{ id: "reference-document" }];
  state.images = images; state.referenceFiles = files;
  const resolvers: Array<(response: Response) => void> = [];
  globalThis.fetch = (() => new Promise(resolve => resolvers.push(resolve))) as any;
  const a = feature.applyGenerationPreset("a");
  const b = feature.applyGenerationPreset("b");
  resolvers[1]!(new Response(JSON.stringify({ preset: preset("b", 2) })));
  await b;
  resolvers[0]!(new Response(JSON.stringify({ preset: preset("a") })));
  await a;
  assert.equal(state.parameterDraftsByModel[model.id]["output.count"], 2);
  assert.equal(state.appliedGenerationPresetId, "b");
  assert.equal(state.images, images); assert.equal(state.referenceFiles, files);
  assert.equal(prompt, "Private prompt that must survive");
  assert.equal(state.generationPresetPending, false);
});

test("editing while an apply is pending cancels that apply without changing newer work", async () => {
  await load();
  let resolve: (response: Response) => void = () => {};
  globalThis.fetch = (() => new Promise(done => { resolve = done; })) as any;
  const applying = feature.applyGenerationPreset("a");
  prompt = "newer prompt";
  resolve(new Response(JSON.stringify({ preset: preset("a") })));
  await applying;
  assert.equal(state.parameterDraftsByModel[model.id]["output.count"], 2);
  assert.equal(prompt, "newer prompt");
  assert.match(elements.get("generationPresetStatus")!.textContent, /变化|changed/);
});

test("unknown binding, unknown parameters and invalid parameter values cannot mutate settings", () => {
  const before = JSON.stringify(state.parameterDraftsByModel);
  for (const invalid of [
    { ...preset("bad"), binding_id: "missing" },
    { ...preset("bad"), canonical_model_id: "missing" },
    { ...preset("bad"), parameters: { secret_parameter: "ignore-me" } },
    { ...preset("bad"), parameters: { "output.count": 999 } },
  ]) assert.throws(() => feature.commitGenerationPreset(invalid as any));
  assert.equal(JSON.stringify(state.parameterDraftsByModel), before);
});

test("delete cancellation, Escape and closing the panel make no request and preserve selection", async () => {
  await load(); select("a");
  let requests = 0;
  globalThis.fetch = (async () => { requests += 1; throw new Error("unexpected request"); }) as any;
  for (const dismiss of [() => feature.cancelPresetDelete(), () => elements.get("generationPresetsPanel")!.fire("keydown", { key: "Escape" }), () => elements.get("generationPresetsPanel")!.fire("toggle")]) {
    feature.requestPresetDelete();
    assert.equal(elements.get("generationPresetDeleteConfirmation")!.hidden, false);
    dismiss();
    await feature.confirmPresetDelete();
    assert.equal(elements.get("generationPresetDeleteConfirmation")!.hidden, true);
    assert.equal(elements.get("generationPresetSelect")!.value, "a");
  }
  assert.equal(requests, 0);
});

test("confirmed deletion targets the originally confirmed preset, and failure does not remove it", async () => {
  await load(); select("a"); feature.requestPresetDelete();
  const calls: string[] = [];
  globalThis.fetch = (async (url: string) => { calls.push(url); return new Response(JSON.stringify({ detail: { message: "fixture failure" } }), { status: 503 }); }) as any;
  await feature.confirmPresetDelete();
  assert.equal(calls[0], "/api/generation-presets/a?confirm=true");
  assert.equal(elements.get("generationPresetSelect")!.value, "a");
  assert.match(elements.get("generationPresetStatus")!.textContent, /fixture failure/);
  feature.cancelPresetDelete();
});

test("load failures are visible and reload recovers without changing composer", async () => {
  const before = JSON.stringify(state.parameterDraftsByModel);
  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: { message: "store unavailable" } }), { status: 503 })) as any;
  await feature.refreshGenerationPresets();
  assert.match(elements.get("generationPresetStatus")!.textContent, /store unavailable/);
  await load();
  assert.equal(JSON.stringify(state.parameterDraftsByModel), before);
  assert.equal(elements.get("generationPresetStatus")!.dataset.kind, "ok");
});

test("namespaced main-model IDs match backend validation without permitting paths or tokens", () => {
  for (const id of ["gpt-5", "openai/gpt-5", "vendor/model-2026.10"]) assert.equal(feature.presetMainModelValid(id), true, id);
  for (const id of ["/etc/passwd", "../gpt-5", "openai/../gpt-5", "https://provider/model", "sk-secret", "vendor/sk_secret", "C:\\model", "a".repeat(129), "openai//gpt-5"]) assert.equal(feature.presetMainModelValid(id), false, id);
});

test("namespaced GPT response options survive saving and applying a preset", () => {
  const bridge = (globalThis as any).window.__codexImageWebUI;
  const previous = { ...state, parameterDraftsByModel: { ...state.parameterDraftsByModel } };
  const gpt = { ...model, id: "gpt-image-2", family_id: "gpt-image" };
  state.generationCatalog = { ...catalog, models: [gpt], providers: [{ id: "provider-a", available: true, bindings: [{ id: "responses", canonical_model_id: gpt.id, operations: ["generate"], protocol_profile: "openai_responses" }] }] };
  bridge.els.mainModel = { value: "old-main-model" };
  bridge.els.promptFidelity = { value: "off" };
  methods.currentMainModel = () => bridge.els.mainModel.value;
  methods.currentPromptFidelity = () => bridge.els.promptFidelity.value;
  try {
    feature.commitGenerationPreset({ ...preset("namespaced"), canonical_model_id: gpt.id, binding_id: "responses", request_options: { main_model: "openai/gpt-5", prompt_fidelity: "strict" } } as any);
    assert.equal(bridge.els.mainModel.value, "openai/gpt-5");
    assert.equal(bridge.els.promptFidelity.value, "strict");
    const saved = feature.currentPresetPayload("Namespaced GPT configuration");
    assert.deepEqual(saved.request_options, { main_model: "openai/gpt-5", prompt_fidelity: "strict" });
  } finally {
    Object.assign(state, previous);
    delete bridge.els.mainModel;
    delete bridge.els.promptFidelity;
    delete methods.currentMainModel;
    delete methods.currentPromptFidelity;
  }
});

test("create and rename send different bounded payloads, with no implicit preset overwrite", async () => {
  await load();
  elements.get("generationPresetName")!.value = "新预设";
  const calls: Array<{ url: string; method: string; payload: any }> = [];
  globalThis.fetch = (async (url: string, options: any) => {
    const payload = JSON.parse(options.body);
    calls.push({ url, method: options.method, payload });
    const item = { ...preset("created"), name: payload.name };
    return new Response(JSON.stringify({ preset: item, presets: [item] }));
  }) as any;
  await feature.saveGenerationPreset();
  assert.equal(calls[0]!.method, "POST");
  assert.equal(calls[0]!.payload.name, "新预设");
  assert.equal(elements.get("generationPresetSelect")!.value, "created");
  elements.get("generationPresetName")!.value = "重命名预设";
  await feature.saveGenerationPreset(true);
  assert.equal(calls[1]!.method, "PATCH");
  assert.equal(calls[1]!.url, "/api/generation-presets/created");
  assert.deepEqual(calls[1]!.payload, { name: "重命名预设" });
});
