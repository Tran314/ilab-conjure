import { getLegacyBridge } from "./state";
import { formatTranslation, LOCALE_CHANGE_EVENT, translate } from "./i18n";
import { GenerationPreviewState, submissionSnapshotKey, type FinalGenerationPreview, type SubmissionSnapshot } from "./generation-preview-state";

const preview = new GenerationPreviewState();
let timer: ReturnType<typeof setTimeout> | null = null;
let pending: { key: string; promise: Promise<FinalGenerationPreview | null>; controller: AbortController } | null = null;

function summaryStatus(message: string, kind: string): void {
  const element = document.getElementById("executionSummary");
  if (element) {
    element.textContent = message;
    element.title = message;
    element.dataset.previewState = kind;
    // Older client summaries placed canonical parameters in data attributes.
    delete element.dataset.parameters;
  }
  const details = document.getElementById("executionSummaryDetails");
  if (details) details.dataset.previewState = kind;
}
function clearSummaryDetails(): void {
  document.getElementById("executionSummaryValues")?.replaceChildren();
}
function summaryLine(label: string, value: unknown): void {
  const list = document.getElementById("executionSummaryValues");
  if (!list || value === undefined || value === null || value === "") return;
  const item = document.createElement("li");
  item.textContent = `${translate(label)}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`;
  list.append(item);
}
function renderFinalSummary(result: FinalGenerationPreview): void {
  const s = result.summary;
  clearSummaryDetails();
  summaryStatus([translate("generationPreview.ready"), s.model?.name || s.model?.id, s.provider?.name || s.provider?.id,
    formatTranslation("ux.imageCount", { count: s.output_count }),
  ].filter(Boolean).join(" · "), "ready");
  summaryLine("generationPreview.operation", translate(s.operation === "edit" ? "generationPresets.edit" : "generationPresets.generate"));
  summaryLine("generationPreview.model", `${s.model?.name || s.model?.id || ""} (${s.model?.id || ""})`);
  summaryLine("generationPreview.provider", `${s.provider?.name || s.provider?.id || ""} (${s.provider?.id || ""})`);
  summaryLine("generationPreview.binding", [s.binding?.id, s.binding?.remote_model_id, s.binding?.protocol_profile].filter(Boolean).join(" · "));
  const decided = translate("generationPreview.providerDecides");
  const dimensions = s.dimensions || {};
  summaryLine("generationPreview.output", [dimensions.size, dimensions.aspect_ratio, dimensions.resolution].filter(value => value !== null && value !== undefined && value !== "").join(" · ") || decided);
  summaryLine("generationPreview.quality", s.quality ?? decided);
  const model = getLegacyBridge().state.generationCatalog?.models.find(item => item.id === s.model?.id);
  for (const [key, value] of Object.entries(s.effective_parameters || {})) {
    const definition = model?.parameters.find(item => item.id === key);
    summaryLine(definition?.label_key || key, value === null ? decided : value);
  }
  summaryLine("generationPreview.requestOptions", s.request_options);
  summaryLine("generationPreview.counts", formatTranslation("generationPreview.countValues", { images: s.output_count, tasks: s.task_count, requests: s.request_count }));
  const queue = s.queue || {};
  summaryLine("generationPreview.queue", formatTranslation("generationPreview.queueValues", {
    provider: queue.provider_concurrency, max: queue.max_provider_concurrency, task: queue.task_request_concurrency,
    auto: translate(queue.auto_start ? "generationPreview.yes" : "generationPreview.no"),
  }));
  const refs = s.references || {};
  summaryLine("generationPreview.imageOrder", (refs.images || []).map((ref: any) => `${ref.position}. ${translate(`generationPreview.source.${ref.kind}`)} #${Number(ref.source_index ?? ref.index ?? 0) + 1}`).join(" → ") || translate("generationPreview.none"));
  summaryLine("generationPreview.fileOrder", (refs.files || []).map((ref: any) => `${ref.position}. ${translate(`generationPreview.source.${ref.kind}`)} #${Number(ref.source_index ?? ref.index ?? 0) + 1}`).join(" → ") || translate("generationPreview.none"));
  const rules = s.reference_rules || refs.rules || getLegacyBridge().state.generationReferenceRules;
  if (rules) summaryLine("generationPreview.referenceRules", `${translate("generationPresets.imageRule")}: ${translate(`generationPresets.${rules.images}`)}; ${translate("generationPresets.fileRule")}: ${translate(`generationPresets.${rules.files}`)}`);
  const limits = refs.limits || {};
  summaryLine("generationPreview.referenceLimits", formatTranslation("generationPreview.limitValues", {
    images: limits.max_images, files: translate(limits.supports_reference_files ? "generationPreview.yes" : "generationPreview.no"),
    mask: translate(limits.supports_mask ? "generationPreview.yes" : "generationPreview.no"),
  }));
  summaryLine("generationPreview.ordering", translate(refs.ordering === "explicit_first_occurrence" ? "generationPreview.orderExplicit" : "generationPreview.orderDefault"));
  summaryLine("generationPreview.fileOrdering", translate("generationPreview.fileOrderRule"));
  if (refs.mask_present) summaryLine("generationPreview.mask", translate("generationPreview.present"));
  (s.warnings || []).forEach((warning: any) => summaryLine("generationPreview.warning", warning.message || warning));
}
function summarySnapshot(): SubmissionSnapshot | null {
  const { state, methods } = getLegacyBridge();
  if (!state.generationCatalog || !state.selectedModelId || !state.selectedProviderId || state.generationPresetApplying || state.generationPresetPending) return null;
  return methods.buildSubmissionRequest?.() || null;
}
export function invalidateExecutionSummary(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  pending?.controller.abort();
  pending = null;
  preview.invalidate();
  clearSummaryDetails();
  summaryStatus(translate("generationPreview.stale"), "stale");
}
function summaryErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : translate("generationPreview.failed");
}
async function requestFinalPreview(request: SubmissionSnapshot): Promise<FinalGenerationPreview | null> {
  if (!String(request.form.get("prompt") || "").trim()) {
    invalidateExecutionSummary();
    summaryStatus(translate("generationPreview.needsPrompt"), "stale");
    return null;
  }
  const key = submissionSnapshotKey(request);
  if (pending?.key === key) return pending.promise;
  pending?.controller.abort();
  const revision = preview.begin(key);
  const controller = new AbortController();
  clearSummaryDetails();
  summaryStatus(translate("generationPreview.loading"), "loading");
  const timeout = setTimeout(() => controller.abort(), 30_000);
  const promise = (async () => {
    try {
      const response = await fetch(`${request.endpoint}/preview`, { method: "POST", body: request.form, signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(String(data.detail?.message || data.detail || translate("generationPreview.failed")).replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 500));
      if (!data.preview_token || !data.summary || typeof data.summary !== "object") throw new Error(translate("generationPreview.failed"));
      const latest = summarySnapshot();
      if (!latest || submissionSnapshotKey(latest) !== key) return null;
      if (!preview.accept(revision, key, data)) return null;
      renderFinalSummary(data);
      return data as FinalGenerationPreview;
    } catch (error) {
      if (revision === preview.revision) {
        clearSummaryDetails();
        summaryStatus(`${translate("generationPreview.failed")}: ${summaryErrorMessage(error)}`, "error");
      }
      return null;
    } finally {
      clearTimeout(timeout);
      if (pending?.controller === controller) pending = null;
    }
  })();
  pending = { key, promise, controller };
  return promise;
}
export function updateExecutionSummary(): void {
  if (getLegacyBridge().state.generationPresetApplying) return;
  const request = summarySnapshot();
  if (!request) { invalidateExecutionSummary(); return; }
  const key = submissionSnapshotKey(request);
  if (!String(request.form.get("prompt") || "").trim()) {
    invalidateExecutionSummary();
    summaryStatus(translate("generationPreview.needsPrompt"), "stale");
    return;
  }
  if (preview.current(key) || pending?.key === key) return;
  invalidateExecutionSummary();
  // Clear stale information synchronously; only network work is debounced.
  timer = setTimeout(() => { timer = null; void requestFinalPreview(request); }, 450);
}
/** A click may submit only a summary the user could already review. */
export async function ensureExecutionPreview(request: SubmissionSnapshot): Promise<string | null> {
  const key = submissionSnapshotKey(request);
  const current = preview.current(key);
  if (current) return current.preview_token;
  if (timer) clearTimeout(timer);
  timer = null;
  const result = await requestFinalPreview(request);
  if (result) {
    const details = document.getElementById("executionSummaryDetails") as HTMLDetailsElement | null;
    if (details) details.open = true;
    getLegacyBridge().methods.setStatus?.(translate("generationPreview.reviewAgain"), "");
  } else {
    getLegacyBridge().methods.setStatus?.(translate("generationPreview.notReady"), "error");
  }
  return null;
}
export function initExecutionSummaryFeature(): void {
  Object.assign(getLegacyBridge().methods, { ensureExecutionPreview, invalidateExecutionSummary });
  document.getElementById("refreshExecutionSummary")?.addEventListener("click", () => {
    invalidateExecutionSummary();
    const request = summarySnapshot();
    if (request) void requestFinalPreview(request);
  });
  document.addEventListener(LOCALE_CHANGE_EVENT, () => {
    const current = summarySnapshot();
    if (current && preview.current(submissionSnapshotKey(current))) renderFinalSummary(preview.result!);
    else updateExecutionSummary();
  });
}
