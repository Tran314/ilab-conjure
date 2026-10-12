export interface SubmissionSnapshot {
  endpoint: string;
  form: FormData;
  inputFiles?: Blob[];
}
export interface FinalGenerationPreview {
  preview_token: string;
  summary: Record<string, any>;
}
const previewInputFileIds = new WeakMap<Blob, number>();
let nextFileId = 0;
function fileId(file: Blob): number {
  if (!previewInputFileIds.has(file)) previewInputFileIds.set(file, ++nextFileId);
  return previewInputFileIds.get(file)!;
}
/** In-memory only: the key is never rendered, persisted, or sent as a preset. */
export function submissionSnapshotKey(snapshot: SubmissionSnapshot): string {
  return JSON.stringify([snapshot.endpoint, Array.from(snapshot.form.entries()).map(([key, value]) => [key,
    typeof value === "string" ? value : [value.name, value.type, value.size],
  ]), (snapshot.inputFiles || []).map(fileId)]);
}

/** A response belongs only to the exact input revision which requested it. */
export class GenerationPreviewState {
  revision = 0;
  key = "";
  result: FinalGenerationPreview | null = null;
  resolvedAt = 0;
  invalidate(key = ""): void {
    this.revision += 1;
    this.key = key;
    this.result = null;
    this.resolvedAt = 0;
  }
  begin(key: string): number {
    this.invalidate(key);
    return this.revision;
  }
  accept(revision: number, key: string, result: FinalGenerationPreview): boolean {
    if (revision !== this.revision || key !== this.key) return false;
    this.result = result;
    this.resolvedAt = Date.now();
    return true;
  }
  current(key: string): FinalGenerationPreview | null {
    return key === this.key && Date.now() - this.resolvedAt < 240_000 ? this.result : null;
  }
}
