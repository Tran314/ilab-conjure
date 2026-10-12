import { getLegacyBridge } from "./state";
import { LOCALE_CHANGE_EVENT, translate } from "./i18n";

/** Read-only view: no repair, restart, log upload or export controls. */
export function initLocalDiagnostics(): void {
  const panel = getLegacyBridge().els.systemSettingsStoragePanel as HTMLElement | null;
  if (!panel || document.getElementById("localDiagnostics")) return;
  const details = document.createElement("details");
  details.id = "localDiagnostics";
  details.className = "settings-previous-paths";
  const summary = document.createElement("summary");
  summary.dataset.i18n = "diagnostics.title";
  summary.textContent = translate("diagnostics.title");
  const description = document.createElement("p");
  description.dataset.i18n = "diagnostics.readOnly";
  description.textContent = translate("diagnostics.readOnly");
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.className = "ghost-button text-sm";
  refresh.dataset.i18n = "diagnostics.refresh";
  refresh.textContent = translate("diagnostics.refresh");
  const content = document.createElement("div");
  content.setAttribute("role", "status");
  content.setAttribute("aria-live", "polite");
  let pending = false;
  async function load(): Promise<void> {
    if (pending) return;
    pending = true;
    refresh.disabled = true;
    content.textContent = translate("diagnostics.loading");
    try {
      const response = await fetch("/api/diagnostics", { cache: "no-store" });
      if (!response.ok) throw new Error("diagnostics_unavailable");
      const data = await response.json();
      const list = document.createElement("dl");
      function row(label: string, value: unknown): void {
        const term = document.createElement("dt");
        term.textContent = label;
        const detail = document.createElement("dd");
        detail.style.overflowWrap = "anywhere";
        detail.textContent = String(value ?? translate("diagnostics.unavailable"));
        list.append(term, detail);
      }
      row(translate("diagnostics.version"), `${data.version} · ${data.mode}`);
      row(translate("diagnostics.runtime"), `${data.runtime?.platform} / ${data.runtime?.architecture} · Python ${data.runtime?.python}`);
      row(translate("diagnostics.executable"), data.runtime?.executable);
      row(translate("diagnostics.service"), data.service?.reachable ? translate("diagnostics.reachable") : translate("diagnostics.unavailable"));
      for (const key of ["data", "input", "output", "source_data"]) {
        const directory = data.directories?.[key];
        if (!directory) continue;
        row(translate(`diagnostics.directory.${key}`), `${directory.path} · ${translate(directory.writable ? "diagnostics.writable" : "diagnostics.notWritable")} (${directory.permissions || "?"})`);
      }
      row(translate("diagnostics.queue"), `${data.queue?.health} · ${translate("diagnostics.waiting")}: ${data.queue?.waiting ?? "?"} · ${translate("diagnostics.running")}: ${data.queue?.running ?? "?"}`);
      row(translate("diagnostics.proxy"), `${data.proxy?.mode} · ${translate(data.proxy?.configured ? "diagnostics.configured" : "diagnostics.notConfigured")}`);
      for (const error of data.recent_errors || []) row(`${error.code} × ${error.count}`, error.next_step);
      for (const step of data.next_steps || []) row(translate("diagnostics.nextStep"), step);
      content.replaceChildren(list);
    } catch {
      content.textContent = translate("diagnostics.failed");
    } finally { pending = false; refresh.disabled = false; }
  }
  refresh.addEventListener("click", () => { void load(); });
  details.addEventListener("toggle", () => { if (details.open) void load(); });
  window.addEventListener(LOCALE_CHANGE_EVENT, () => { if (details.open) void load(); });
  details.append(summary, description, refresh, content);
  panel.append(details);
}
