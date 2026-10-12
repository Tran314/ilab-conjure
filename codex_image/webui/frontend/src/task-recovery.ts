import { translate } from "./i18n";
export type TaskRecovery = "credentials" | "quota" | "input" | "temporary";
export type SafeRetryAction = "retry_submit" | "resume_poll" | "retry_download" | "retry_save" | "confirm_regenerate" | "none";
const SAFE_ACTIONS = new Set<SafeRetryAction>(["retry_submit", "resume_poll", "retry_download", "retry_save", "confirm_regenerate", "none"]);
export function taskSafeRetryAction(task: any): SafeRetryAction | null {
  const value = task?.safe_retry_action || task?.generation_error?.safe_retry_action;
  if (SAFE_ACTIONS.has(value)) return value;
  const outputs = Array.isArray(task?.outputs) ? task.outputs.filter((output: any) => output.status !== "completed") : [];
  for (const action of ["confirm_regenerate", "retry_save", "retry_download", "resume_poll", "retry_submit", "none"] as SafeRetryAction[]) {
    if (outputs.some((output: any) => (output.safe_retry_action || output.generation_error?.safe_retry_action) === action)) return action;
  }
  return null;
}
export function taskResultUnknown(task: any): boolean {
  if (task?.submission_unknown) return true;
  if (task?.status && !["failed", "partial_failed", "cancelled"].includes(task.status) && !task.cancelled_at) return false;
  return Boolean(task?.result_certainty === "submitted_unknown"
    || task?.generation_error?.result_certainty === "submitted_unknown"
    || (Array.isArray(task?.outputs) && task.outputs.some((output: any) => output.result_certainty === "submitted_unknown")));
}
export function taskRecoveryActionLabel(task: any): string {
  const action = taskSafeRetryAction(task);
  return translate(action ? `recovery.action.${action}` : "preview.retryFailed");
}
export function taskRecoveryKind(task: any): TaskRecovery {
  const code = task?.generation_error?.code || task?.outputs?.find((output: any) => output.generation_error)?.generation_error?.code;
  if (["authentication_failed", "provider_credentials_missing", "provider_credentials_origin_changed"].includes(code)) return "credentials";
  if (["insufficient_quota", "quota_exceeded"].includes(code)) return "quota";
  if (["invalid_parameters", "operation_unsupported", "input_limit_exceeded", "snapshot_manifest_incompatible"].includes(code)) return "input";
  if (code) return "temporary";
  const text = String(task?.error || task?.last_error || "").toLowerCase();
  const statusMatch = text.match(/\bhttp(?:\s+(?:error|status))?\s*[:=]?\s*(\d{3})\b/)
    || text.match(/\bstatus(?:\s+code)?\s*[:=]\s*(\d{3})\b/);
  const status = statusMatch ? Number(statusMatch[1]) : null;
  if (status === 401 || status === 403) return "credentials";
  if (status !== null && status >= 500 && status <= 599) return "temporary";
  if (status === null && /invalid_api_key|authentication_error|unauthorized|incorrect api key/.test(text)) return "credentials";
  if (/quota|usage limit|insufficient_quota|billing/.test(text)) return "quota";
  if (status === 400 || status === 422) return "input";
  if (status === null && /invalid[_ ](?:parameters?|value)|unsupported mime|base64-encoded data url/.test(text)) return "input";
  return "temporary";
}
export function taskRecoveryMessage(task: any): string {
  const action = taskSafeRetryAction(task);
  if (taskResultUnknown(task)) return translate(task?.cancelled_at || task?.cancel_requested ? "recovery.cancelledUnknown" : "recovery.resultUnknown");
  if (["retry_save", "retry_download", "resume_poll"].includes(action || "")) return translate(`recovery.guidance.${action}`);
  return translate(`ux.recovery.${taskRecoveryKind(task)}`);
}
export function localizedTaskStatus(status: string): string {
  return translate(status === "partial_failed" ? "taskStatus.partialFailed" : `taskStatus.${status}`);
}
