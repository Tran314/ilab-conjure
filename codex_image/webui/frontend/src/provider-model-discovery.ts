import { formatTranslation, LOCALE_CHANGE_EVENT, translate } from "./i18n";
import { destroyThemedSelects, mountThemedSelect, syncThemedSelect } from "./themed-select";

export interface ModelDiscoveryConnection {
  provider_id: string;
  api_key_source_provider_id: string;
  base_url: string;
  api_key: string;
}

const ERROR_TRANSLATIONS: Record<string, string> = {
  invalid_base_url: "apiSettings.modelsInvalidBaseUrl",
  api_key_origin_mismatch: "apiSettings.modelsOriginMismatch",
  model_discovery_key_required: "apiSettings.modelsKeyRequired",
  model_discovery_unauthorized: "apiSettings.modelsUnauthorized",
  model_discovery_not_supported: "apiSettings.modelsNotSupported",
  model_discovery_rate_limited: "apiSettings.modelsRateLimited",
  model_discovery_invalid_response: "apiSettings.modelsInvalidResponse",
  model_discovery_too_large: "apiSettings.modelsTooLarge",
};

export async function fetchProviderModels(
  connection: ModelDiscoveryConnection,
  protocol: string,
  signal?: AbortSignal,
  request: typeof fetch = fetch,
): Promise<string[]> {
  const response = await request("/api/api-settings/models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...connection, protocol }),
    signal: signal ?? null,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(ERROR_TRANSLATIONS[payload?.detail] || "apiSettings.modelsFetchFailed");
  }
  if (!Array.isArray(payload?.models)) throw new Error("apiSettings.modelsInvalidResponse");
  return [...new Set<string>(payload.models
    .map((model: any) => typeof model?.id === "string" ? model.id.trim() : "")
    .filter(Boolean))];
}

export function createModelDiscoveryField(input: HTMLInputElement, bindingId: string): HTMLElement {
  const field = document.createElement("div");
  field.className = "field provider-binding-remote-model";
  const heading = document.createElement("div");
  heading.className = "provider-model-discovery-heading";
  const label = document.createElement("label");
  input.id = `provider-binding-${bindingId}-remote-model`;
  label.htmlFor = input.id;
  label.dataset.i18n = "apiSettings.remoteModelName";
  label.textContent = translate("apiSettings.remoteModelName");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost-button provider-model-discovery-button";
  button.dataset.fetchProviderModels = "";
  button.dataset.i18n = "apiSettings.fetchModels";
  button.textContent = translate("apiSettings.fetchModels");
  heading.append(label, button);
  const results = document.createElement("div");
  results.className = "provider-model-discovery-results";
  results.dataset.providerModelsResults = "";
  results.hidden = true;
  const select = document.createElement("select");
  select.className = "control";
  select.dataset.providerModelsSelect = "";
  select.setAttribute("aria-label", translate("apiSettings.selectAvailableModel"));
  results.append(select);
  const status = document.createElement("p");
  status.className = "provider-model-discovery-status";
  status.dataset.providerModelsStatus = "";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.hidden = true;
  field.append(heading, input, results, status);
  return field;
}

export function initProviderModelDiscovery(context: {
  container: HTMLElement | null;
  connectionInputs: Array<HTMLInputElement | null>;
  getConnection(): ModelDiscoveryConnection;
}): void {
  const { container } = context;
  if (!container) return;
  const requests = new WeakMap<HTMLElement, AbortController>();
  const fingerprints = new WeakMap<HTMLElement, string>();
  const fingerprint = (card: HTMLElement) => JSON.stringify({
    ...context.getConnection(),
    protocol: card.querySelector<HTMLSelectElement>("[data-binding-protocol]")?.value,
  });
  const setStatus = (card: HTMLElement, message: string, error = false) => {
    const status = card.querySelector<HTMLElement>("[data-provider-models-status]");
    if (!status) return;
    status.textContent = message;
    status.hidden = !message;
    status.classList.toggle("error", error);
  };
  const setBusy = (button: HTMLButtonElement, busy: boolean) => {
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
    button.dataset.i18n = busy ? "apiSettings.fetchingModels" : "apiSettings.fetchModels";
    button.textContent = translate(button.dataset.i18n);
  };
  const clear = (card: HTMLElement) => {
    requests.get(card)?.abort();
    requests.delete(card);
    fingerprints.delete(card);
    const results = card.querySelector<HTMLElement>("[data-provider-models-results]");
    destroyThemedSelects(results);
    if (results) results.hidden = true;
    const button = card.querySelector<HTMLButtonElement>("[data-fetch-provider-models]");
    if (button) setBusy(button, false);
    setStatus(card, "");
  };
  const clearAll = () => container.querySelectorAll<HTMLElement>("[data-binding-id]").forEach(clear);
  context.connectionInputs.forEach((input) => {
    input?.addEventListener("input", clearAll);
    input?.addEventListener("change", clearAll);
  });
  document.addEventListener(LOCALE_CHANGE_EVENT, clearAll);

  container.addEventListener("click", async (event) => {
    const button = (event.target as HTMLElement | null)?.closest<HTMLButtonElement>("[data-fetch-provider-models]");
    const card = button?.closest<HTMLElement>("[data-binding-id]");
    if (!button || !card || button.disabled) return;
    clear(card);
    const controller = new AbortController();
    const connectionFingerprint = fingerprint(card);
    requests.set(card, controller);
    setBusy(button, true);
    setStatus(card, translate("apiSettings.fetchingModels"));
    try {
      const protocol = card.querySelector<HTMLSelectElement>("[data-binding-protocol]")?.value || "";
      const models = await fetchProviderModels(context.getConnection(), protocol, controller.signal);
      if (!container.contains(card) || requests.get(card) !== controller || fingerprint(card) !== connectionFingerprint) return;
      const results = card.querySelector<HTMLElement>("[data-provider-models-results]");
      const select = card.querySelector<HTMLSelectElement>("[data-provider-models-select]");
      if (!select || !results) return;
      if (!models.length) {
        setStatus(card, translate("apiSettings.modelsEmpty"));
        return;
      }
      const placeholder = new Option(translate("apiSettings.selectAvailableModel"), "");
      placeholder.disabled = true;
      select.replaceChildren(placeholder, ...models.map((id) => new Option(id, id)));
      select.value = "";
      select.setAttribute("aria-label", translate("apiSettings.selectAvailableModel"));
      results.hidden = false;
      mountThemedSelect(select);
      fingerprints.set(card, connectionFingerprint);
      setStatus(card, formatTranslation("apiSettings.modelsFetched", { count: models.length }));
    } catch (error) {
      if (controller.signal.aborted || !container.contains(card) || requests.get(card) !== controller || fingerprint(card) !== connectionFingerprint) return;
      const key = error instanceof Error && error.message.startsWith("apiSettings.")
        ? error.message : "apiSettings.modelsFetchFailed";
      setStatus(card, translate(key), true);
    } finally {
      if (requests.get(card) === controller) {
        requests.delete(card);
        setBusy(button, false);
      }
    }
  });
  container.addEventListener("input", (event) => {
    const input = event.target as HTMLInputElement | null;
    if (!input?.matches("[data-binding-remote-model]")) return;
    const select = input.closest("[data-binding-id]")?.querySelector<HTMLSelectElement>("[data-provider-models-select]");
    if (!select) return;
    select.value = [...select.options].some((option) => option.value === input.value) ? input.value : "";
    syncThemedSelect(select);
  });
  container.addEventListener("change", (event) => {
    const target = event.target as HTMLSelectElement | null;
    const card = target?.closest<HTMLElement>("[data-binding-id]");
    if (!target || !card) return;
    if (target.matches("[data-binding-model], [data-binding-protocol], [data-binding-compatibility]")) {
      clear(card);
    } else if (target.matches("[data-provider-models-select]") && target.value) {
      if (fingerprints.get(card) !== fingerprint(card)) { clear(card); return; }
      const input = card.querySelector<HTMLInputElement>("[data-binding-remote-model]");
      if (!input) return;
      input.value = target.value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
}
