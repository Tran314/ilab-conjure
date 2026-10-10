import { translate } from "./i18n";
import type { HistoryTask } from "./history-types";
import { localizedTaskStatus } from "./task-recovery";

export function historyEmptyImageLabel(task: Pick<HistoryTask, "status" | "generated_count">): string {
  if (task.generated_count > 0) return translate("history.media.unavailable");
  if (["queued", "running", "submitting", "cancelling"].includes(task.status)) return localizedTaskStatus(task.status);
  return translate(task.status === "failed" ? "history.media.failed" : "history.media.empty");
}

/** Image errors alone cannot establish that an original file is missing. */
export function missingOriginalResponse(status: number, detail: unknown): boolean {
  return status === 404 && detail === "Output not found";
}

export function bindHistoryThumbnailStates(root: HTMLElement, signal: AbortSignal): void {
  root.addEventListener("load", (event) => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || !image.hasAttribute("data-history-thumbnail")) return;
    const frame = image.closest<HTMLElement>(".history-task-thumb-frame");
    if (frame) frame.dataset.mediaState = "loaded";
  }, { capture: true, signal });

  root.addEventListener("error", (event) => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || !image.hasAttribute("data-history-thumbnail")) return;
    const frame = image.closest<HTMLElement>(".history-task-thumb-frame");
    const label = frame?.querySelector<HTMLElement>("[data-history-media-label]");
    if (!frame || !label) return;
    const url = image.src;
    frame.dataset.mediaState = "unavailable";
    label.textContent = translate("history.media.unavailable");
    image.remove();
    // Only re-read a failed thumbnail route; never download originals or retry generation.
    void fetch(url, { signal }).then(async (response) => {
      if (response.status !== 404) return;
      const payload = await response.json().catch(() => null);
      if (!frame.isConnected || !missingOriginalResponse(response.status, payload?.detail)) return;
      frame.dataset.mediaState = "missing";
      label.textContent = translate("history.media.missing");
    }).catch(() => { /* Keep the recoverable load-error state on network failure. */ });
  }, { capture: true, signal });
}
