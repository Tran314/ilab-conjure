import assert from "node:assert/strict";
import test from "node:test";
import { initComposerDraft, preserveComposerDraft, restoreComposerDraft } from "../../codex_image/webui/frontend/src/composer-draft";

function setup(store: any, search = "") {
  let prompt = "";
  let status = "";
  const events: Record<string, Function> = {};
  const navigations: string[] = [];
  const restoreButton: any = { hidden: true, addEventListener() {} };
  const state: any = { images: [], referenceFiles: [], mode: "generate", taskInputRestoreSeq: 0 };
  const methods: any = {
    getPromptText: () => prompt,
    setPromptText: (value: string) => { prompt = value; },
    setMode: (value: string) => { state.mode = value; },
    setStatus: (value: string) => { status = value; },
  };
  const els: any = {};
  (globalThis as any).window = {
    __codexImageWebUI: { state, methods, els },
    location: { href: `http://localhost/${search}`, origin: "http://localhost", search, assign: (url: string) => navigations.push(url) },
    addEventListener: (name: string, fn: Function) => { events[name] = fn; },
  };
  (globalThis as any).document = {
    getElementById: () => restoreButton,
    addEventListener: (name: string, fn: Function) => { events[name] = fn; },
  };
  (globalThis as any).localStorage = { getItem: () => null };
  initComposerDraft(store);
  const anchor: any = { href: "http://localhost/history", target: "", hasAttribute: () => false, setAttribute() {}, removeAttribute() {} };
  return {
    state, methods, els, navigations, restoreButton,
    get prompt() { return prompt; }, set prompt(value: string) { prompt = value; },
    get status() { return status; },
    click: (options: any = {}) => {
      const event = { button: 0, defaultPrevented: false, target: { closest: () => anchor }, preventDefault() { this.defaultPrevented = true; }, ...options };
      events.click?.(event);
      return event;
    },
    warned: () => {
      let warned = false;
      events.beforeunload?.({ preventDefault: () => { warned = true; } });
      return warned;
    },
    pageshow: () => events.pageshow?.({ persisted: true }),
  };
}

function memoryStore() {
  let saved: any = null;
  return {
    hasPending: () => saved !== null,
    save: async (snapshot: any) => { saved = snapshot; },
    load: async () => saved,
    clear: async () => { saved = null; },
    get saved() { return saved; },
  };
}
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

test("internal history saves text, image blobs, reference files and earlier drafts before navigating", async () => {
  const store = memoryStore();
  const page = setup(store);
  page.prompt = "Earlier draft";
  preserveComposerDraft();
  const image = new File(["image bytes"], "synthetic.png", { type: "image/png" });
  const file = new File(["reference bytes"], "synthetic.txt", { type: "text/plain" });
  page.prompt = "Current @reference #ffffff ~snippet";
  page.state.images = [{ kind: "upload", name: image.name, file: image, previewUrl: URL.createObjectURL(image) }];
  page.state.referenceFiles = [{ kind: "upload", filename: file.name, file }];
  assert.equal(page.warned(), true);
  assert.equal(page.click().defaultPrevented, true);
  await settle();
  assert.deepEqual(page.navigations, ["http://localhost/history"]);
  assert.equal(page.warned(), false);
  assert.equal(store.saved.current.images[0].file, image);
  assert.equal(store.saved.current.files[0].file, file);
  assert.equal(store.saved.drafts[0].prompt, "Earlier draft");
  URL.revokeObjectURL(page.state.images[0].previewUrl);

  const returned = setup(store);
  await returned.methods.restoreComposerNavigationDraft();
  assert.equal(returned.prompt, "Current @reference #ffffff ~snippet");
  assert.equal(await (await fetch(returned.state.images[0].previewUrl)).text(), "image bytes");
  assert.equal(await returned.state.referenceFiles[0].file.text(), "reference bytes");
  assert.equal(store.hasPending(), false);
  assert.equal(returned.warned(), true, "external leaving still protects the restored draft");
  restoreComposerDraft();
  assert.equal(returned.prompt, "Earlier draft");
});

test("save failure keeps inputs and the original leave warning", async () => {
  const store = memoryStore();
  store.save = async () => { throw new Error("Quota exceeded"); };
  const page = setup(store);
  page.prompt = "Keep this input";
  page.click();
  await settle();
  assert.deepEqual(page.navigations, []);
  assert.equal(page.prompt, "Keep this input");
  assert.equal(page.warned(), true);
  assert.ok(page.status);
});

test("an edit during saving is saved again and repeated clicks navigate once", async () => {
  const store = memoryStore();
  const save = store.save;
  let release!: () => void;
  let saves = 0;
  store.save = async snapshot => {
    saves++;
    if (saves === 1) await new Promise<void>(resolve => { release = resolve; });
    await save(snapshot);
  };
  const page = setup(store);
  page.prompt = "First";
  page.click();
  page.click();
  page.prompt = "Latest";
  release();
  await settle();
  assert.equal(saves, 2);
  assert.equal(store.saved.current.prompt, "Latest");
  assert.equal(page.navigations.length, 1);
  page.prompt = "After saving";
  assert.equal(page.warned(), true);
});

test("new-tab clicks and unsaved provider or image edits retain existing behavior", async () => {
  const store = memoryStore();
  const page = setup(store);
  page.prompt = "Dirty composer";
  assert.equal(page.click({ ctrlKey: true }).defaultPrevented, false);
  assert.equal(page.click({ metaKey: true }).defaultPrevented, false);
  page.state.apiProviderEditingId = "provider-1";
  assert.equal(page.click().defaultPrevented, false);
  assert.equal(page.warned(), true);
  page.state.apiProviderEditingId = null;
  page.els.imageEditorModal = { classList: { contains: () => false } };
  assert.equal(page.click().defaultPrevented, false);
  assert.equal(page.warned(), true);
  await settle();
  assert.equal(store.hasPending(), false);
});

test("explicit history reuse and edits during loading keep the original input recoverable", async () => {
  for (const handoff of [true, false]) {
    const store = memoryStore();
    const page = setup(store);
    page.prompt = "Original draft";
    page.click();
    await settle();
    const returned = setup(store);
    if (handoff) (globalThis as any).localStorage = { getItem: () => "pending handoff" };
    const restoring = returned.methods.restoreComposerNavigationDraft();
    if (!handoff) returned.prompt = "Typed while loading";
    await restoring;
    assert.equal(returned.prompt, handoff ? "" : "Typed while loading");
    assert.equal(returned.restoreButton.hidden, false);
    restoreComposerDraft();
    assert.equal(returned.prompt, "Original draft");
  }
});

test("failed restoration retains the temporary copy and cached browser back resets the bypass", async () => {
  const store = memoryStore();
  const page = setup(store);
  page.prompt = "Retained draft";
  page.click();
  await settle();
  page.pageshow();
  assert.equal(page.warned(), true);
  await settle();
  assert.equal(store.hasPending(), false);
  await store.save({ current: { prompt: "Retained draft", images: [], files: [], mode: "generate" }, baseline: "", drafts: [] });
  store.load = async () => { throw new Error("Read failed"); };
  const returned = setup(store);
  await returned.methods.restoreComposerNavigationDraft();
  assert.equal(store.hasPending(), true);
  assert.equal(returned.warned(), true);
  assert.ok(returned.status);
});

test("a history click waits for pending restoration instead of replacing it with an empty composer", async () => {
  const store = memoryStore();
  const page = setup(store);
  page.prompt = "Original input";
  page.click();
  await settle();
  const load = store.load;
  let release!: () => void;
  store.load = async () => {
    await new Promise<void>(resolve => { release = resolve; });
    return load();
  };
  const returned = setup(store);
  const restoring = returned.methods.restoreComposerNavigationDraft();
  returned.click();
  assert.deepEqual(returned.navigations, []);
  release();
  await restoring;
  await settle();
  assert.equal(store.saved.current.prompt, "Original input");
  assert.equal(returned.navigations.length, 1);
});

test("cached back cleanup finishes before another history snapshot is saved", async () => {
  const store = memoryStore();
  const page = setup(store);
  page.prompt = "Cached input";
  page.click();
  await settle();
  const clear = store.clear;
  let release!: () => void;
  store.clear = async () => {
    await new Promise<void>(resolve => { release = resolve; });
    await clear();
  };
  page.pageshow();
  page.click();
  release();
  await settle();
  assert.equal(store.saved.current.prompt, "Cached input");
  assert.equal(page.navigations.length, 2);
});
