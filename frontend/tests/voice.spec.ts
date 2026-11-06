import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/#token=offline-console-test");
  await page.getByRole("link", { name: "模型与能力" }).click();
  await page.getByRole("button", { name: "语音", exact: true }).click();
});

test("voice saves independently, keeps credentials private and previews only on demand", async ({ page }) => {
  let previews = 0;
  page.on("request", request => { if (request.url().endsWith("/api/voice/test")) previews++; });
  const secret = "synthetic-voice-credential-never-sent";
  await page.getByLabel("官方语音 API Key").fill(secret);
  await page.getByRole("switch", { name: "启用语音", exact: true }).check();
  await page.getByLabel("音色", { exact: true }).selectOption("cedar");
  await expect(page.getByLabel("说话语气")).toHaveCount(0);
  await page.getByLabel("额外识别纠错词（可选）").fill("艾尔登法环");
  await expect(page.getByRole("button", { name: "试听并验证识别" })).toBeDisabled();
  expect(await page.evaluate(() => JSON.stringify(sessionStorage) + JSON.stringify(localStorage))).not.toContain(secret);
  const save = page.waitForRequest(r => r.url().endsWith("/api/voice") && r.method() === "POST");
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  const data = (await save).postDataJSON();
  expect(data.voice).toBe("cedar");
  expect(data.api_key).toBe(secret);
  expect(data).not.toHaveProperty("api_key_saved");
  expect(data).not.toHaveProperty("style");
  await expect(page.getByText("语音设置已生效", { exact: true })).toBeVisible();
  await expect(page.getByLabel("官方语音 API Key")).toHaveValue("");
  expect(previews).toBe(0);
  const publicConfig = await (await page.request.get("/api/voice")).json();
  expect(JSON.stringify(publicConfig)).not.toContain(secret);
  await page.getByLabel("试听人格").selectOption("30002");
  const previewRequest = page.waitForRequest(r => r.url().endsWith("/api/voice/test"));
  await page.getByRole("button", { name: "试听并验证识别" }).click();
  expect((await previewRequest).postDataJSON()).toMatchObject({group_id:"30002", chat_revision:publicConfig.chat_revision});
  await expect(page.getByLabel("语音试听")).toBeVisible();
  await expect.poll(() => page.getByLabel("语音试听").evaluate((audio: HTMLAudioElement) => audio.readyState)).toBeGreaterThanOrEqual(1);
  await page.getByLabel("语音试听").evaluate(async (audio: HTMLAudioElement) => { await audio.play(); audio.pause(); });
  await expect(page.locator('.voice-preview')).toContainText("今天群里挺热闹的");
  expect(previews).toBe(1);
  await page.screenshot({ path: "../.local/console-browser-results/voice-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "../.local/console-browser-results/voice-mobile.png", fullPage: true });
});

test("voice conflicts keep the draft and do not silently test unsaved changes", async ({ page }) => {
  await page.getByLabel("额外识别纠错词（可选）").fill("只狼");
  await page.getByRole("button", { name: "模型分工", exact: true }).click();
  await page.getByRole("button", { name: "语音", exact: true }).click();
  await expect(page.getByLabel("额外识别纠错词（可选）")).toHaveValue("只狼");
  await page.route("**/api/voice", async route => {
    if (route.request().method() === "POST") await route.fulfill({status: 409, json: { error: "语音设置已变化，请刷新后再操作。" }});
    else await route.continue();
  });
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  await expect(page.getByText("语音设置已变化，请刷新后再操作。", { exact: true })).toBeVisible();
  await expect(page.getByLabel("额外识别纠错词（可选）")).toHaveValue("只狼");
  await expect(page.getByRole("button", { name: "试听并验证识别" })).toBeDisabled();
});
