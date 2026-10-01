import assert from "node:assert/strict";
import test from "node:test";
import { fetchProviderModels, initProviderModelDiscovery } from "../../codex_image/webui/frontend/src/provider-model-discovery";

const connection = {
  provider_id: "draft", api_key_source_provider_id: "source",
  base_url: "https://relay.example/custom", api_key: "",
};

test("discovery uses the draft connection without saving or choosing a model", async () => {
  const requests: Array<{ path: string; options: RequestInit }> = [];
  const request = (async (path: string, options: RequestInit) => {
    requests.push({ path, options });
    return new Response(JSON.stringify({ models: [{ id: "vendor/custom:2" }, { id: "other" }, { id: "other" }] }));
  }) as typeof fetch;
  const controller = new AbortController();
  assert.deepEqual(await fetchProviderModels(connection, "gemini", controller.signal, request), ["vendor/custom:2", "other"]);
  assert.equal(requests.length, 1);
  assert.equal(requests[0]?.path, "/api/api-settings/models");
  assert.equal(requests[0]?.options.signal, controller.signal);
  assert.deepEqual(JSON.parse(String(requests[0]?.options.body)), { ...connection, protocol: "gemini" });
  assert.equal(connection.api_key, "");
});

test("empty lists succeed while malformed success is rejected", async () => {
  const request = (async () => new Response(JSON.stringify({ models: [] }))) as typeof fetch;
  assert.deepEqual(await fetchProviderModels(connection, "openai_images", undefined, request), []);
  const malformed = (async () => new Response("<html>wrong</html>")) as typeof fetch;
  await assert.rejects(fetchProviderModels(connection, "openai_images", undefined, malformed), /modelsInvalidResponse/);
});

test("known failures offer specific feedback and unknown provider errors stay private", async () => {
  for (const [detail, key] of [
    ["model_discovery_key_required", "modelsKeyRequired"],
    ["api_key_origin_mismatch", "modelsOriginMismatch"],
    ["model_discovery_unauthorized", "modelsUnauthorized"],
    ["model_discovery_not_supported", "modelsNotSupported"],
    ["secret echoed by provider", "modelsFetchFailed"],
  ]) {
    const request = (async () => new Response(JSON.stringify({ detail }), { status: 502 })) as typeof fetch;
    await assert.rejects(fetchProviderModels(connection, "openai_images", undefined, request), new RegExp(String(key)));
  }
});

test("aborted fetch propagates cancellation", async () => {
  const request = (async () => { throw new DOMException("Cancelled", "AbortError"); }) as typeof fetch;
  await assert.rejects(fetchProviderModels(connection, "openai_images", undefined, request), { name: "AbortError" });
});

// Exercise the async controller separately from the shared themed-select renderer.
async function withController(run: (fixture: any) => Promise<void>): Promise<void> {
  const listeners = new Map<string, Array<(event: any) => unknown>>();
  let invalidateConnection = () => {};
  let fetchCount = 0;
  let requestSignal: AbortSignal | null = null;
  let resolveResponse!: (response: Response) => void;
  const response = new Promise<Response>((resolve) => { resolveResponse = resolve; });
  const button: any = {
    disabled: false, dataset: {}, textContent: "", setAttribute() {},
    closest: (selector: string) => selector.includes("fetch-provider-models") ? button : card,
  };
  const status = { textContent: "", hidden: true, classList: { toggle() {} } };
  const results = { hidden: true, querySelectorAll: () => [] };
  const remote = { value: "manual/custom" };
  const card: any = {
    querySelector: (selector: string) => ({
      "[data-fetch-provider-models]": button,
      "[data-provider-models-status]": status,
      "[data-provider-models-results]": results,
      "[data-provider-models-select]": {},
      "[data-binding-protocol]": { value: "openai_images" },
      "[data-binding-remote-model]": remote,
    })[selector],
  };
  const draft = { ...connection };
  const container: any = {
    addEventListener(type: string, listener: (event: any) => unknown) {
      listeners.set(type, [...(listeners.get(type) || []), listener]);
    },
    querySelectorAll: () => [card],
    contains: (target: any) => target === card,
  };
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const previousFetch = globalThis.fetch;
  Object.defineProperty(globalThis, "document", { configurable: true, value: { addEventListener() {} } });
  globalThis.fetch = (async (_url: any, options: RequestInit) => {
    fetchCount += 1;
    requestSignal = options.signal as AbortSignal;
    return response; // Intentionally ignores abort to simulate a late response.
  }) as typeof fetch;
  try {
    initProviderModelDiscovery({
      container,
      connectionInputs: [{ addEventListener: (_type: string, handler: () => void) => { invalidateConnection = handler; } } as HTMLInputElement],
      getConnection: () => draft,
    });
    await run({
      button, status, results, remote, draft,
      click: () => Promise.all((listeners.get("click") || []).map((listener) => listener({ target: button }))),
      invalidate: () => invalidateConnection(),
      resolve: (models: Array<{ id: string }>) => resolveResponse(new Response(JSON.stringify({ models }))),
      fetchCount: () => fetchCount,
      requestSignal: () => requestSignal,
    });
  } finally {
    globalThis.fetch = previousFetch;
    if (previousDocument) Object.defineProperty(globalThis, "document", previousDocument);
    else delete (globalThis as any).document;
  }
}

test("controller ignores duplicate requests while busy and preserves manual input", async () => {
  await withController(async (fixture) => {
    const pending = fixture.click();
    assert.equal(fixture.button.disabled, true);
    await fixture.click();
    assert.equal(fixture.fetchCount(), 1);
    fixture.resolve([]);
    await pending;
    assert.equal(fixture.button.disabled, false);
    assert.equal(fixture.status.hidden, false);
    assert.equal(fixture.remote.value, "manual/custom");
  });
});

test("connection changes abort the request and prevent late results from appearing", async () => {
  await withController(async (fixture) => {
    const pending = fixture.click();
    fixture.draft.base_url = "https://other.example/v1";
    fixture.invalidate();
    assert.equal(fixture.requestSignal().aborted, true);
    fixture.resolve([{ id: "stale-model" }]);
    await pending;
    assert.equal(fixture.results.hidden, true);
    assert.equal(fixture.status.hidden, true);
    assert.equal(fixture.button.disabled, false);
    assert.equal(fixture.remote.value, "manual/custom");
  });
});
