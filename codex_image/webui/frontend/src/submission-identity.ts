import { sha256 } from "@noble/hashes/sha2.js";

const STORAGE_PREFIX = "ilab-conjure-pending-submission-v1:";
export interface SubmissionIdentity { requestId: string; fingerprint: string }
function hex(bytes: Uint8Array): string {
  return Array.from(sha256(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
}
function remember(endpoint: string, fields: unknown[], storage: Storage): SubmissionIdentity {
  const fingerprint = hex(new TextEncoder().encode(JSON.stringify({ endpoint, fields })));
  const key = STORAGE_PREFIX + fingerprint;
  const existing = storage.getItem(key);
  const requestId = typeof existing === "string" && /^[A-Za-z0-9_.-]{16,128}$/.test(existing)
    ? existing
    : globalThis.crypto?.randomUUID?.() || `submission-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  // Persist before sending. If storage is denied/full, fail before any paid work.
  // Separate keys avoid one tab overwriting another unresolved submission.
  storage.setItem(key, requestId);
  return { requestId, fingerprint };
}

/** Reuse only an unresolved, byte-identical submission, including after reload. */
export function prepareSubmissionIdentity(
  endpoint: string, form: FormData, storage: Storage = window.localStorage,
): SubmissionIdentity | Promise<SubmissionIdentity> {
  const fields: Array<[string, unknown]> = [];
  const pending: Promise<void>[] = [];
  Array.from(new Set(form.keys())).sort().forEach(key => {
    if (key === "client_request_id") return;
    form.getAll(key).forEach(value => {
      if (typeof value === "string") { fields.push([key, value]); return; }
      const details = { filename: value.name, content_type: value.type, sha256: "" };
      fields.push([key, details]);
      pending.push(value.arrayBuffer().then(bytes => { details.sha256 = hex(new Uint8Array(bytes)); }));
    });
  });
  return pending.length
    ? Promise.all(pending).then(() => remember(endpoint, fields, storage))
    : remember(endpoint, fields, storage);
}

export function completeSubmissionIdentity(identity: SubmissionIdentity, storage: Storage = window.localStorage): void {
  const key = STORAGE_PREFIX + identity.fingerprint;
  if (storage.getItem(key) === identity.requestId) storage.removeItem(key);
}
