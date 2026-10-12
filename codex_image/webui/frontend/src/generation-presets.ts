import { isGptImageModel } from "./gpt-image-models";
import { selectedProviderBinding } from "./provider-selection";
import { currentGenerationSelection } from "./generation-request";
import { activeParameterValuesFor, parameterValueValid } from "./model-parameters";
import { eligibleProviderBindings, providerBindingSelectionKey } from "./provider-selection";
import { getLegacyBridge } from "./state";
import { formatTranslation, LOCALE_CHANGE_EVENT, translate } from "./i18n";
import { submissionSnapshotKey } from "./generation-preview-state";
import type { GenerationOperation } from "./types";

export interface ReferenceRules {
  images: "preserve" | "require" | "forbid";
  files: "preserve" | "require" | "forbid";
  order: "preserve";
}
export interface GenerationPreset {
  id: string;
  name: string;
  operation: GenerationOperation;
  canonical_model_id: string;
  provider_id: string;
  binding_id: string;
  parameters: Record<string, unknown>;
  reference_rules: ReferenceRules;
  request_options?: { main_model?: string; prompt_fidelity?: "strict" | "original" | "off" };
  availability?: { available: boolean; issues: Array<{ code: string; message: string }> };
}
const DEFAULT_RULES: ReferenceRules = { images: "preserve", files: "preserve", order: "preserve" };
let presets: GenerationPreset[] = [];
let selectedId = "";
let applySequence = 0;
let loadSequence = 0;
let mutationPending = false;
let deleteId = "";

function presetElement<T extends HTMLElement = HTMLElement>(id: string): T | null { return document.getElementById(id) as T | null; }
function presetStatus(text: string, error = false): void {
  const status = presetElement("generationPresetStatus");
  if (status) { status.textContent = text; status.dataset.kind = error ? "error" : "ok"; }
}
function selectedPreset(): GenerationPreset | undefined { return presets.find(preset => preset.id === selectedId); }
function presetErrorMessage(error: unknown): string { return (error instanceof Error ? error.message : translate("generationPresets.failed")).replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 500); }
async function presetApi(path = "", options: RequestInit = {}): Promise<any> {
  const response = await fetch(`/api/generation-presets${path}`, { ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) } });
  const data = await response.json();
  if (!response.ok) throw new Error(String(data.detail?.message || data.detail || translate("generationPresets.failed")));
  return data;
}
export function validatePresetName(value: string, exceptId = ""): string {
  const name = value.normalize("NFKC").trim();
  if (!name || [...name].length > 120 || /[\u0000-\u001f\u007f]/.test(name)) throw new Error(translate("generationPresets.invalidName"));
  if (presets.some(item => item.id !== exceptId && item.name.normalize("NFKC").trim().toLocaleLowerCase() === name.toLocaleLowerCase())) throw new Error(translate("generationPresets.duplicateName"));
  return name;
}
function presetInputRules(): ReferenceRules {
  return {
    images: (presetElement<HTMLSelectElement>("generationPresetImageRule")?.value || "preserve") as ReferenceRules["images"],
    files: (presetElement<HTMLSelectElement>("generationPresetFileRule")?.value || "preserve") as ReferenceRules["files"],
    order: "preserve",
  };
}
/** Explicit allowlist: no prompt, inputs, provider credentials, or local paths. */
export function currentPresetPayload(name: string): Omit<GenerationPreset, "id"> {
  const { state, methods } = getLegacyBridge();
  const selection = currentGenerationSelection();
  const requestOptions = isGptImageModel(selection.canonicalModelId) ? {
    prompt_fidelity: methods.currentPromptFidelity?.() || "strict",
    ...(selectedProviderBinding()?.protocol_profile.endsWith("responses") ? { main_model: methods.currentMainModel?.() || "" } : {}),
  } : {};
  return {
    name: validatePresetName(name),
    operation: state.mode,
    canonical_model_id: selection.canonicalModelId,
    provider_id: selection.providerId,
    binding_id: selection.bindingId,
    parameters: selection.parameters,
    reference_rules: presetInputRules(),
    request_options: requestOptions,
  };
}
function renderPresetPanel(): void {
  const select = presetElement<HTMLSelectElement>("generationPresetSelect");
  if (select) {
    select.replaceChildren();
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = translate(presets.length ? "generationPresets.choose" : "generationPresets.empty");
    select.append(empty);
    presets.forEach(preset => {
      const option = document.createElement("option");
      option.value = preset.id;
      option.textContent = `${preset.name}${preset.availability?.available === false ? ` (${translate("generationPresets.unavailable")})` : ""}`;
      option.title = preset.name;
      select.append(option);
    });
    if (!presets.some(preset => preset.id === selectedId)) selectedId = "";
    select.value = selectedId;
    select.title = selectedPreset()?.name || "";
    select.disabled = mutationPending;
  }
  for (const id of ["applyGenerationPreset", "renameGenerationPreset", "deleteGenerationPreset"]) {
    const button = presetElement<HTMLButtonElement>(id);
    if (button) button.disabled = mutationPending || !selectedPreset();
  }
  for (const id of ["confirmGenerationPresetDelete", "cancelGenerationPresetDelete"]) {
    const button = presetElement<HTMLButtonElement>(id);
    if (button) button.disabled = mutationPending;
  }
  const save = presetElement<HTMLButtonElement>("saveGenerationPreset");
  if (save) save.disabled = mutationPending;
}
export async function refreshGenerationPresets(): Promise<void> {
  const sequence = ++loadSequence;
  presetStatus(translate("generationPresets.loading"));
  try {
    const data = await presetApi();
    if (sequence !== loadSequence) return;
    if (!Array.isArray(data.presets)) throw new Error(translate("generationPresets.failed"));
    presets = data.presets;
    renderPresetPanel();
    presetStatus(presets.length ? translate("generationPresets.hint") : translate("generationPresets.empty"));
  } catch (error) {
    if (sequence !== loadSequence) return;
    presetStatus(`${translate("generationPresets.loadFailed")}: ${presetErrorMessage(error)}`, true);
  }
}
function selectedPresetDescription(): void {
  cancelPresetDelete();
  const preset = selectedPreset();
  const name = presetElement<HTMLInputElement>("generationPresetName");
  if (name) name.value = preset?.name || "";
  if (preset) {
    const imageRule = presetElement<HTMLSelectElement>("generationPresetImageRule");
    const fileRule = presetElement<HTMLSelectElement>("generationPresetFileRule");
    if (imageRule) imageRule.value = preset.reference_rules.images;
    if (fileRule) fileRule.value = preset.reference_rules.files;
    presetStatus(preset.availability?.available === false ? preset.availability.issues.map(issue => issue.message).join("; ") : translate("generationPresets.hint"), preset.availability?.available === false);
  }
  renderPresetPanel();
}
export function presetMainModelValid(value: string): boolean {
  return value.length <= 128 && /^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\/[A-Za-z0-9][A-Za-z0-9_.-]*)*$/.test(value)
    && value.split("/").every(segment => !/^sk[-_]/i.test(segment));
}
export function validatePresetForComposer(preset: GenerationPreset): void {
  const { state } = getLegacyBridge();
  const catalog = state.generationCatalog;
  const model = catalog?.models.find(item => item.id === preset.canonical_model_id);
  if (!catalog || !model || !model.operations.includes(preset.operation)) throw new Error(translate("generationPresets.unknownModel"));
  const binding = eligibleProviderBindings(catalog, model.id, preset.operation).find(item => item.provider.id === preset.provider_id && item.binding.id === preset.binding_id);
  if (!binding) throw new Error(translate("generationPresets.unknownProvider"));
  const active = activeParameterValuesFor(model, preset.operation, preset.parameters);
  for (const [key, value] of Object.entries(preset.parameters)) {
    const definition = model.parameters.find(item => item.id === key);
    if (!definition || !parameterValueValid(definition, value) || !(key in active)) throw new Error(formatTranslation("generationPresets.invalidParameter", { parameter: key }));
  }
  const options = preset.request_options || {};
  if (Object.keys(options).some(key => !["main_model", "prompt_fidelity"].includes(key))
    || (options.main_model !== undefined && (!isGptImageModel(model.id) || !binding.binding.protocol_profile.endsWith("responses") || !presetMainModelValid(options.main_model)))
    || (options.prompt_fidelity !== undefined && (!isGptImageModel(model.id) || !["strict", "original", "off"].includes(options.prompt_fidelity)))) throw new Error(translate("generationPresets.incompatible"));
  const rules = preset.reference_rules;
  if (!rules || !["preserve", "require", "forbid"].includes(rules.images) || !["preserve", "require", "forbid"].includes(rules.files) || rules.order !== "preserve") throw new Error(translate("generationPresets.invalidRules"));
}
/** Single synchronous commit. Rendering is batched so intermediate controls cannot leak into a preview. */
export function commitGenerationPreset(preset: GenerationPreset): void {
  validatePresetForComposer(preset);
  const { state, els, methods } = getLegacyBridge();
  const previousOptions = { main_model: els.mainModel?.value, prompt_fidelity: els.promptFidelity?.value };
  const model = state.generationCatalog!.models.find(item => item.id === preset.canonical_model_id)!;
  const fields = ["mode", "selectedModelId", "selectedFamilyId", "selectedProviderId", "selectedProviderBindingId", "authAvailable", "lastModelByFamily", "lastProviderByModel", "lastProviderSelectionByModel", "parameterDraftsByModel", "parameterDraftVersionsByModel", "generationReferenceRules", "appliedGenerationPresetId"];
  const previous = Object.fromEntries(fields.map(key => [key, state[key]]));
  const redraw = () => {
    document.querySelectorAll<HTMLElement>("[data-mode]").forEach(button => button.classList.toggle("active", button.dataset.mode === state.mode));
    methods.renderModelSelectors?.();
    methods.renderProviderSelection?.();
    methods.restoreCurrentModelParameterDraft?.();
    methods.syncRunButtonLabel?.();
    methods.updateModeSpecificSettings?.();
    methods.reconcileTaskParameterInspection?.();
    methods.refreshOutputSettingsLock?.();
  };
  state.generationPresetApplying = true;
  methods.invalidateExecutionSummary?.();
  try {
    Object.assign(state, {
      mode: preset.operation,
      selectedModelId: model.id,
      selectedFamilyId: model.family_id,
      selectedProviderId: preset.provider_id,
      selectedProviderBindingId: preset.binding_id,
      authAvailable: true,
      lastModelByFamily: { ...state.lastModelByFamily, [model.family_id]: model.id },
      lastProviderByModel: { ...state.lastProviderByModel, [model.id]: preset.provider_id },
      lastProviderSelectionByModel: { ...state.lastProviderSelectionByModel, [model.id]: providerBindingSelectionKey(preset.provider_id, preset.binding_id) },
      parameterDraftsByModel: { ...state.parameterDraftsByModel, [model.id]: { ...Object.fromEntries(model.parameters.map(definition => [definition.id, definition.default])), ...JSON.parse(JSON.stringify(preset.parameters)) } },
      parameterDraftVersionsByModel: { ...state.parameterDraftVersionsByModel, [model.id]: model.version },
      generationReferenceRules: { ...preset.reference_rules },
      appliedGenerationPresetId: preset.id,
    });
    if (preset.request_options?.main_model !== undefined && els.mainModel) els.mainModel.value = preset.request_options.main_model;
    if (preset.request_options?.prompt_fidelity !== undefined && els.promptFidelity) els.promptFidelity.value = preset.request_options.prompt_fidelity;
    redraw();
    const actual = currentGenerationSelection();
    if (actual.canonicalModelId !== model.id || actual.providerId !== preset.provider_id || actual.bindingId !== preset.binding_id || Object.entries(preset.parameters).some(([key, value]) => JSON.stringify(actual.parameters[key]) !== JSON.stringify(value))) throw new Error(translate("generationPresets.incompatible"));
    methods.persistModelSelection?.();
    if (preset.request_options?.main_model !== undefined) methods.persistMainModel?.();
  } catch (error) {
    Object.assign(state, previous);
    if (els.mainModel) els.mainModel.value = previousOptions.main_model;
    if (els.promptFidelity) els.promptFidelity.value = previousOptions.prompt_fidelity;
    redraw();
    throw error;
  } finally {
    state.generationPresetApplying = false;
    methods.updateRequestPreview?.();
  }
}
function presetComposerKey(): string {
  const { state, methods } = getLegacyBridge();
  const request = methods.buildSubmissionRequest?.();
  return request ? submissionSnapshotKey(request) : JSON.stringify([state.mode, currentGenerationSelection(), state.images, state.referenceFiles]);
}
export async function applyGenerationPreset(id = selectedId): Promise<void> {
  const preset = presets.find(item => item.id === id);
  if (!preset) return;
  const sequence = ++applySequence;
  const { state, methods } = getLegacyBridge();
  methods.syncPromptFromEditor?.();
  methods.syncGalleryInputsFromPrompt?.();
  const key = presetComposerKey();
  state.generationPresetPending = true;
  methods.invalidateExecutionSummary?.();
  presetStatus(translate("generationPresets.applying"));
  try {
    validatePresetForComposer(preset);
    const data = await presetApi(`/${encodeURIComponent(id)}/apply`, { method: "POST", body: JSON.stringify({ image_count: state.images.length, reference_file_count: state.referenceFiles.length, has_mask: false }) });
    if (sequence !== applySequence) return;
    if (key !== presetComposerKey()) throw new Error(translate("generationPresets.changedDuringApply"));
    commitGenerationPreset(data.preset);
    selectedId = id;
    const options = data.preset.request_options || {};
    const retainedOptions = isGptImageModel(data.preset.canonical_model_id) && (!options.prompt_fidelity
      || selectedProviderBinding()?.protocol_profile.endsWith("responses") && !options.main_model);
    presetStatus(formatTranslation("generationPresets.applied", { name: data.preset.name })
      + (retainedOptions ? ` ${translate("generationPresets.retainedOptions")}` : ""));
    renderPresetPanel();
  } catch (error) {
    if (sequence === applySequence) presetStatus(presetErrorMessage(error), true);
  } finally {
    if (sequence === applySequence) {
      state.generationPresetPending = false;
      methods.updateRequestPreview?.();
    }
  }
}
export async function saveGenerationPreset(rename = false): Promise<void> {
  if (mutationPending) return;
  const preset = selectedPreset();
  if (rename && !preset) return;
  try {
    const name = validatePresetName(presetElement<HTMLInputElement>("generationPresetName")?.value || "", rename ? preset!.id : "");
    const body = rename ? { name } : currentPresetPayload(name);
    mutationPending = true;
    renderPresetPanel();
    const data = await presetApi(rename ? `/${encodeURIComponent(preset!.id)}` : "", { method: rename ? "PATCH" : "POST", body: JSON.stringify(body) });
    ++loadSequence;
    presets = data.presets;
    selectedId = data.preset.id;
    presetStatus(translate("generationPresets.saved"));
  } catch (error) { presetStatus(presetErrorMessage(error), true); }
  finally { mutationPending = false; renderPresetPanel(); }
}
export function requestPresetDelete(): void {
  const preset = selectedPreset();
  if (!preset || mutationPending) return;
  deleteId = preset.id;
  const confirmation = presetElement("generationPresetDeleteConfirmation");
  if (confirmation) confirmation.hidden = false;
  const text = presetElement("generationPresetDeleteQuestion");
  if (text) text.textContent = formatTranslation("generationPresets.deleteQuestion", { name: preset.name });
  presetElement("cancelGenerationPresetDelete")?.focus();
}
export function cancelPresetDelete(): void {
  deleteId = "";
  const confirmation = presetElement("generationPresetDeleteConfirmation");
  if (confirmation) confirmation.hidden = true;
}
export async function confirmPresetDelete(): Promise<void> {
  if (!deleteId || mutationPending) return;
  const id = deleteId;
  mutationPending = true;
  renderPresetPanel();
  try {
    const data = await presetApi(`/${encodeURIComponent(id)}?confirm=true`, { method: "DELETE" });
    ++loadSequence;
    presets = data.presets;
    if (selectedId === id) selectedId = "";
    cancelPresetDelete();
    presetStatus(translate("generationPresets.deleted"));
  } catch (error) { presetStatus(presetErrorMessage(error), true); }
  finally { mutationPending = false; renderPresetPanel(); }
}
export function initGenerationPresetsFeature(): void {
  Object.assign(getLegacyBridge().methods, { refreshGenerationPresets, applyGenerationPreset });
  presetElement("generationPresetSelect")?.addEventListener("change", () => {
    selectedId = presetElement<HTMLSelectElement>("generationPresetSelect")?.value || "";
    selectedPresetDescription();
  });
  presetElement("applyGenerationPreset")?.addEventListener("click", () => void applyGenerationPreset());
  presetElement("saveGenerationPreset")?.addEventListener("click", () => void saveGenerationPreset());
  presetElement("renameGenerationPreset")?.addEventListener("click", () => void saveGenerationPreset(true));
  presetElement("deleteGenerationPreset")?.addEventListener("click", requestPresetDelete);
  presetElement("cancelGenerationPresetDelete")?.addEventListener("click", cancelPresetDelete);
  presetElement("confirmGenerationPresetDelete")?.addEventListener("click", () => void confirmPresetDelete());
  presetElement("clearGenerationPresetRules")?.addEventListener("click", () => {
    const { state, methods } = getLegacyBridge();
    state.generationReferenceRules = { ...DEFAULT_RULES };
    state.appliedGenerationPresetId = "";
    methods.updateRequestPreview?.();
    presetStatus(translate("generationPresets.rulesCleared"));
  });
  presetElement("refreshGenerationPresets")?.addEventListener("click", () => void refreshGenerationPresets());
  presetElement("generationPresetsPanel")?.addEventListener("keydown", event => { if (event.key === "Escape") cancelPresetDelete(); });
  presetElement("generationPresetsPanel")?.addEventListener("toggle", () => { if (!(presetElement<HTMLDetailsElement>("generationPresetsPanel")?.open)) cancelPresetDelete(); });
  document.addEventListener(LOCALE_CHANGE_EVENT, renderPresetPanel);
  renderPresetPanel();
}
