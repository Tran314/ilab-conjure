import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { once } from "node:events";
import { randomUUID } from "node:crypto";
import { chromium } from "playwright";

const port = Number(process.env.ILAB_BROWSER_TEST_PORT || 18187);
const base = `http://127.0.0.1:${port}`;
const artifacts = process.env.BROWSER_ARTIFACT_DIR || ".dist/presets-browser";
const instanceToken = randomUUID();
const server = spawn(process.env.PYTHON || "python", ["tests/browser/presets_server.py", "--port", String(port)], {
  cwd: process.cwd(), env: { ...process.env, PYTHONPATH: process.cwd(), ILAB_BROWSER_TEST_TOKEN: instanceToken }, stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
for (const stream of [server.stdout, server.stderr]) stream.on("data", value => { serverLog += value.toString(); });
let browser;
let page;
const errors = [];
const routeErrors = [];
const waitUntil = async (predicate, description, timeout = 20_000) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (routeErrors.length) throw new Error(routeErrors.join("\n"));
    if (await predicate()) return;
    if (server.exitCode !== null) throw new Error(`Test server exited: ${serverLog}`);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${description}`);
};
const state = async () => {
  const data = await (await fetch(`${base}/__qa__/state`)).json();
  assert.equal(data.instance_token, instanceToken, "Refuse to use an unrelated process on the test port");
  return data;
};
try {
  await waitUntil(async () => { try { await state(); return true; } catch { return false; } }, "owned test server readiness");
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    return url.origin === base || ["data:", "blob:"].includes(url.protocol) ? route.continue() : route.abort();
  });
  await page.goto(base);
  await page.locator("#generationPresetsPanel > summary").click();
  await page.locator("#promptEditor").fill("浏览器回归：预设不应修改这段提示词。");
  await page.locator("#executionSummary[data-preview-state='ready']").waitFor();
  assert.equal((await state()).provider_calls, 0);
  assert.equal((await state()).task_ids.length, 0);

  const nameA = "中文长名称".repeat(18);
  await page.locator("#generationPresetName").fill(nameA);
  await page.locator("#saveGenerationPreset").click();
  await waitUntil(async () => (await state()).presets.length === 1, "first preset persisted");
  const first = (await state()).presets[0];
  assert.equal(first.name, nameA);
  assert.equal(JSON.stringify(first).includes("浏览器回归"), false);

  await page.locator("#saveGenerationPreset").click();
  await page.locator("#generationPresetStatus[data-kind='error']").waitFor();
  assert.equal((await state()).presets.length, 1, "duplicate name must not create a record");

  await page.locator("#qualityGroup button[data-val='high']").click();
  await page.locator("#quantityGroup button[data-val='2']").click();
  await page.locator("#executionSummary[data-preview-state='ready']").waitFor();
  await page.locator("#generationPresetName").fill("双图高质量");
  await page.locator("#saveGenerationPreset").click();
  await waitUntil(async () => (await state()).presets.length === 2, "second preset persisted");
  const second = (await state()).presets.find(item => item.name === "双图高质量");
  assert.equal(second.parameters["output.count"], 2);
  assert.equal(second.parameters["gpt.quality"], "high");

  const applyPreset = async preset => {
    await page.locator("#generationPresetSelect").selectOption(preset.id);
    const applied = page.waitForResponse(response => response.url() === `${base}/api/generation-presets/${preset.id}/apply` && response.request().method() === "POST");
    await page.locator("#applyGenerationPreset").click();
    assert.equal((await applied).status(), 200);
    await waitUntil(async () => (await page.locator("#generationPresetStatus[data-kind='ok']").textContent())?.includes(preset.name), "preset committed to composer");
    await page.locator("#executionSummary[data-preview-state='ready']").waitFor();
    assert.equal(await page.locator("#nInput").inputValue(), String(preset.parameters["output.count"]));
  };
  for (const preset of [first, second, first, second]) {
    await applyPreset(preset);
    assert.equal(await page.locator("#promptEditor").innerText(), "浏览器回归：预设不应修改这段提示词。");
  }
  await page.locator("#executionSummaryDetails > summary").click();
  assert.match(await page.locator("#executionSummaryValues").innerText(), /high/);

  await page.locator("#generationPresetName").fill("已重命名的双图预设");
  await page.locator("#renameGenerationPreset").click();
  await waitUntil(async () => (await state()).presets.some(item => item.id === second.id && item.name === "已重命名的双图预设"), "rename persisted");
  await page.locator("#deleteGenerationPreset").click();
  await page.locator("#generationPresetDeleteConfirmation").waitFor({ state: "visible" });
  await page.locator("#cancelGenerationPresetDelete").click();
  assert.equal((await state()).presets.length, 2, "cancel deletion preserves records");
  await page.reload();
  await page.locator("#generationPresetsPanel > summary").click();
  await waitUntil(async () => (await page.locator("#generationPresetSelect option").count()) === 3, "presets survive reload");
  await page.locator("#promptEditor").fill("Submission response loss must keep one task.");
  await page.locator("#executionSummary[data-preview-state='ready']").waitFor();
  await applyPreset({ ...second, name: "已重命名的双图预设" });
  await page.locator("#executionSummary[data-preview-state='ready']").waitFor();

  const acceptedIds = [];
  const clientIds = [];
  let loseResponse = true;
  await page.route(`${base}/api/generate`, async route => {
    try {
      const body = route.request().postDataBuffer()?.toString() || "";
      clientIds.push(body.match(/name="client_request_id"\r?\n\r?\n([^\r\n]+)/)?.[1]);
      const response = await route.fetch();
      const payload = await response.json();
      assert.equal(response.status(), 200, JSON.stringify(payload));
      acceptedIds.push(payload.task.task_id);
      if (loseResponse) { loseResponse = false; await route.abort("timedout"); }
      else await route.fulfill({ response });
    } catch (error) {
      routeErrors.push(String(error));
      await route.abort().catch(() => {});
    }
  });
  await page.locator("#runButton").click();
  await waitUntil(async () => acceptedIds.length === 1, "first submission accepted before response loss");
  await waitUntil(async () => (await page.locator("body").innerText()).includes("提交结果未知") || (await page.locator("body").innerText()).includes("Submission result unknown"), "uncertainty message");
  await page.locator("#runButton").click();
  await waitUntil(async () => acceptedIds.length === 2, "replayed submission recovered");
  assert.equal(acceptedIds[0], acceptedIds[1]);
  assert.ok(clientIds[0]);
  assert.equal(clientIds[0], clientIds[1]);
  assert.equal((await state()).task_ids.length, 1);
  assert.equal((await state()).provider_calls, 0);

  await page.locator("#generationPresetSelect").selectOption(first.id);
  await page.locator("#deleteGenerationPreset").click();
  await page.locator("#confirmGenerationPresetDelete").click();
  await waitUntil(async () => (await state()).presets.length === 1, "confirmed deletion");
  assert.deepEqual(errors, []);
  if (!(await page.locator("#executionSummaryDetails").evaluate(element => element.open))) {
    await page.locator("#executionSummaryDetails > summary").click();
  }
  await mkdir(artifacts, { recursive: true });
  await page.screenshot({ path: `${artifacts}/presets-summary.png`, fullPage: true });
  console.log("Browser QA passed: create/duplicate/long Chinese name/apply/reapply/rename/cancel delete/reload/final summary/lost-response dedupe/confirmed delete; zero provider executions.");
} catch (error) {
  await mkdir(artifacts, { recursive: true });
  if (page) await page.screenshot({ path: `${artifacts}/failure.png`, fullPage: true }).catch(() => {});
  console.error(serverLog);
  throw error;
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  if (server.exitCode === null) await Promise.race([once(server, "exit"), new Promise(resolve => setTimeout(resolve, 10_000))]);
  if (server.exitCode === null) server.kill("SIGKILL");
}
