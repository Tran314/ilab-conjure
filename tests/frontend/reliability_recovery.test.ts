import assert from "node:assert/strict";
import test from "node:test";
import { prepareSubmissionIdentity, completeSubmissionIdentity } from "../../codex_image/webui/frontend/src/submission-identity";
import { taskSafeRetryAction, taskResultUnknown, taskRecoveryKind, taskRecoveryMessage } from "../../codex_image/webui/frontend/src/task-recovery";
import { apiErrorMessage } from "../../codex_image/webui/frontend/src/api-error";

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => { values.set(key, value); }, removeItem: key => { values.delete(key); }, clear: () => values.clear(), key: index => [...values.keys()][index] ?? null, get length() { return values.size; } };
}
function form(prompt = "private prompt"): FormData { const value = new FormData(); value.append("prompt", prompt); return value; }

test("unresolved content reuses persisted identity after reload; success permits a new task", async () => {
  const storage = memoryStorage();
  const first = await prepareSubmissionIdentity("/api/generate", form(), storage);
  const reload = await prepareSubmissionIdentity("/api/generate", form(), storage);
  assert.deepEqual(first, reload);
  assert.notEqual((await prepareSubmissionIdentity("/api/generate", form("new prompt"), storage)).requestId, first.requestId);
  assert.notEqual((await prepareSubmissionIdentity("/api/edit", form(), storage)).requestId, first.requestId);
  assert.ok(!storage.getItem(storage.key(0)!)?.includes("private prompt"));
  completeSubmissionIdentity(first, storage);
  assert.notEqual((await prepareSubmissionIdentity("/api/generate", form(), storage)).requestId, first.requestId);
});

test("file identity includes bytes, not just a matching filename or length", async () => {
  const storage = memoryStorage();
  const first = form(); first.append("images", new File(["red"], "same.png", {type:"image/png"}));
  const same = form(); same.append("images", new File(["red"], "same.png", {type:"image/png"}));
  const changed = form(); changed.append("images", new File(["blu"], "same.png", {type:"image/png"}));
  const identity = await prepareSubmissionIdentity("/api/edit", first, storage);
  assert.equal((await prepareSubmissionIdentity("/api/edit", same, storage)).requestId, identity.requestId);
  assert.notEqual((await prepareSubmissionIdentity("/api/edit", changed, storage)).requestId, identity.requestId);
});

test("unavailable browser storage fails before submission; stale completions do not erase a newer identity", async () => {
  const denied = {getItem: () => null, setItem: () => { throw new Error("storage denied"); }} as any;
  assert.throws(() => prepareSubmissionIdentity("/api/generate", form(), denied), /storage denied/);
  const storage = memoryStorage();
  const first = await prepareSubmissionIdentity("/api/generate", form(), storage);
  completeSubmissionIdentity(first, storage);
  const newer = await prepareSubmissionIdentity("/api/generate", form(), storage);
  completeSubmissionIdentity(first, storage);
  assert.equal((await prepareSubmissionIdentity("/api/generate", form(), storage)).requestId, newer.requestId);
});

test("structured recovery controls take precedence over raw error-text guesses", () => {
  assert.equal(taskRecoveryKind({error:"HTTP 401 token", generation_error:{code:"asset_download_failed"}}), "temporary");
  assert.equal(taskSafeRetryAction({outputs:[{status:"failed", safe_retry_action:"retry_download"},{status:"failed",safe_retry_action:"confirm_regenerate"}]}), "confirm_regenerate");
  assert.equal(taskSafeRetryAction({generation_error:{safe_retry_action:"retry_save"}}), "retry_save");
  assert.equal(taskResultUnknown({cancelled_at:"now",outputs:[{result_certainty:"submitted_unknown"}]}), true);
  assert.match(taskRecoveryMessage({cancelled_at:"now",result_certainty:"submitted_unknown"}), /服务商.*计费/);
  assert.ok(!apiErrorMessage({code:"gallery_item_in_use",message:"raw"}, "fallback").includes("[object Object]"));
  assert.notEqual(apiErrorMessage({code:"gallery_item_in_use",message:"raw"}, "fallback"), "raw");
});

const state: any = {tasks:[]};
const requests: any[] = [];
let confirmation: any = null;
let responseCode = "";
let statusMessage = "";
const methods: any = {
  closePromptPopover() {}, canRetryFailedTask: () => true,
  currentApiProviderId: () => "different-provider",
  renderTasks() {}, renderPreview() {},
  setStatus: (message: string) => { statusMessage = message; },
  openConfirmPopover: (_anchor: any, options: any) => { confirmation = options; },
};
(globalThis as any).window = { __codexImageWebUI:{state,els:{runButton:{}},methods}, refreshQueue:async()=>{} };
(globalThis as any).document = { activeElement:{} };
(globalThis as any).fetch = async (_url: string, options: any) => {
  requests.push(JSON.parse(options.body));
  return responseCode
    ? {ok:false,status:409,json:async()=>({detail:{code:responseCode,message:"The result is unknown."}})}
    : {ok:true,status:200,json:async()=>({task:{task_id:"t",status:"queued"}})};
};
const { initTaskActionsFeature } = await import("../../codex_image/webui/frontend/src/task-actions");
initTaskActionsFeature();
function setup(action: string): void {
  requests.length=0; confirmation=null; responseCode=""; statusMessage="";
  state.tasks=[{task_id:"t",status:"failed",safe_retry_action:action}];
}

test("ambiguous retry opens a cancel-focused warning and sends only after explicit confirmation", async () => {
  setup("confirm_regenerate");
  await methods.retryFailedTask("t");
  assert.equal(requests.length, 0);
  assert.equal(confirmation.focusCancel, true);
  assert.match(confirmation.message, /再次计费/);
  // Dismissing/cancelling the popover sends nothing. Only this callback grants consent.
  await confirmation.onConfirm();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].confirm_regenerate, true);
});

test("safe download recovery does not change providers or authorize regeneration", async () => {
  setup("retry_download");
  await methods.retryFailedTask("t");
  assert.deepEqual(requests, [{}]);
  assert.equal(confirmation, null);
});

test("409 ambiguity does not silently resubmit; explicit regenerate carries both flags", async () => {
  setup("retry_submit"); responseCode="regeneration_confirmation_required";
  await methods.retryFailedTask("t");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].confirm_regenerate, undefined);
  assert.ok(confirmation);
  assert.ok(!statusMessage.includes("[object Object]"));
  responseCode="";
  await confirmation.onConfirm();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].confirm_regenerate, true);
  setup("retry_save");
  await methods.retryFailedTask("t", false, true);
  assert.equal(requests.length,0);
  await confirmation.onConfirm();
  assert.equal(requests[0].confirm_regenerate,true);
  assert.equal(requests[0].regenerate,true);
});


test("frozen task retries and explicit regeneration retain the original provider despite global selection", async () => {
  for (const action of ["retry_submit", "confirm_regenerate", "retry_save"]) {
    setup(action);
    state.tasks[0].generation_snapshot = {provider_id:"original-provider"};
    await methods.retryFailedTask("t");
    if (confirmation) await confirmation.onConfirm();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].api_provider_id, undefined);
  }
  setup("retry_download");
  state.tasks[0].generation_snapshot = {provider_id:"original-provider"};
  await methods.retryFailedTask("t", false, true);
  assert.equal(requests.length,0);
  await confirmation.onConfirm();
  assert.deepEqual(requests[0], {confirm_regenerate:true,regenerate:true});
});
