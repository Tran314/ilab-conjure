import assert from "node:assert/strict";
import test from "node:test";
import { historyEmptyImageLabel, missingOriginalResponse } from "../../codex_image/webui/frontend/src/history-thumbnail-state";
import { translate } from "../../codex_image/webui/frontend/src/i18n";

test("missing media does not turn a completed generation into a failed task", () => {
  assert.equal(historyEmptyImageLabel({ status: "completed", generated_count: 2 }), translate("history.media.unavailable"));
  assert.equal(historyEmptyImageLabel({ status: "failed", generated_count: 0 }), translate("history.media.failed"));
  assert.equal(historyEmptyImageLabel({ status: "completed", generated_count: 0 }), translate("history.media.empty"));
  assert.equal(historyEmptyImageLabel({ status: "running", generated_count: 0 }), translate("taskStatus.running"));
});

test("only the output-specific 404 establishes missing original media", () => {
  assert.equal(missingOriginalResponse(404, "Output not found"), true);
  for (const [status, detail] of [[500, "Output not found"], [404, "Thumbnail unavailable"], [404, "Task not found"], [401, null], [200, "Output not found"]] as const) {
    assert.equal(missingOriginalResponse(status, detail), false);
  }
});
