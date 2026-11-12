import { test, expect } from "@playwright/test";
test.beforeEach(async ({ page }) => {
  await page.goto("/#token=offline-console-test");
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
});

test("comic download progress is visible and the open task follows completion", async ({ page }) => {
  let complete = false;
  await page.route("**/api/tasks?*", async (route) => {
    await route.fulfill({ json: { tasks: [{
      id: "c".repeat(32), kind: "comic_translation", conversation: "group:30001", actor: "10001",
      status: complete ? "completed" : "running", stage: complete ? "completed" : "downloading",
      downloaded_bytes: 8 * 1048576, total_bytes: 24 * 1048576,
      delivery: complete ? "sent" : "pending", created_at: Date.now() / 1000 - 65,
    }], next_before: null } });
  });
  await page.getByRole("link", { name: "任务与记录" }).click();
  await page.getByRole("button", { name: "后台文件任务", exact: true }).click();
  await expect(page.locator("tbody")).toContainText("下载中 · 8.0 / 24.0 MiB");
  await page.locator("tbody tr").first().click();
  await expect(page.getByRole("dialog")).toContainText("下载中 · 8.0 / 24.0 MiB");
  complete = true;
  await expect(page.getByRole("dialog").getByText("已完成", { exact: true }).first()).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole("dialog").getByText("文件已发送", { exact: true })).toBeVisible();
});
test("login exchanges the link, persists a cookie and supports explicit logout", async ({
  page,
  context,
}) => {
  await expect(page).not.toHaveURL(/token=/);
  const cookie = (await context.cookies()).find(
    (v) => v.name === "qunling_session",
  );
  expect(cookie?.httpOnly).toBe(true);
  expect(cookie?.sameSite).toBe("Strict");
  expect(
    await page.evaluate(
      () => JSON.stringify(localStorage) + JSON.stringify(sessionStorage),
    ),
  ).not.toContain("offline-console-test");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "总览", exact: true }),
  ).toBeVisible();
  await page.goto("/#token=offline-console-test");
  await expect(page).not.toHaveURL(/token=/);
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(
    page.getByRole("heading", { name: "打开你的群灵" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "打开你的群灵" }),
  ).toBeVisible();
});
test("group draft survives navigation and reload, trial uses draft, save confirms revision", async ({
  page,
}) => {
  await page.getByRole("link", { name: "群聊与人格" }).click();
  const persona = page.getByLabel("性格与交流习惯", { exact: false });
  await persona.fill("爱接梗，不长篇大论。");
  await page.getByRole("link", { name: "模型与能力" }).click();
  await page.getByRole("link", { name: "群聊与人格" }).click();
  await expect(persona).toHaveValue("爱接梗，不长篇大论。");
  page.on("dialog", (d) => d.accept());
  await page.reload();
  await expect(persona).toHaveValue("爱接梗，不长篇大论。");
  await page.getByLabel("试聊消息").fill("今天终于忙完了");
  const started = page.waitForRequest(
    (r) =>
      r.url().endsWith("/api/preview") && r.postDataJSON()?.action === "start",
  );
  await page.getByRole("button", { name: "发送试聊" }).click();
  expect((await started).postDataJSON().config.groups["30001"].persona).toBe(
    "爱接梗，不长篇大论。",
  );
  await expect(
    page.getByText("忙完了？那今晚可以安心摸鱼了。", { exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "参与方式", exact: true }).click();
  await page.getByRole("tab", { name: "人格", exact: true }).click();
  await expect(
    page.getByText("忙完了？那今晚可以安心摸鱼了。", { exact: true }),
  ).toHaveCount(1);
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  await expect(page.locator(".save-bar")).toHaveCount(0);
  await page.screenshot({
    path: "../.local/console-browser-results/groups-desktop.png",
    fullPage: true,
  });
});
test("records, scoped memory editing, model secrets and native advanced controls", async ({
  page,
}) => {
  await page.getByRole("link", { name: "任务与记录" }).click();
  await page.locator("tbody tr").first().click();
  await expect(
    page.getByRole("dialog").getByText("发送已确认", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("link", { name: "群聊与人格" }).click();
  await page.getByRole("tab", { name: "记忆", exact: true }).click();
  await page.getByRole("button", { name: /阿夏 · 成员资料/ }).click();
  await page.getByLabel("称呼", { exact: true }).fill("夏夏");
  await page.getByRole("button", { name: "保存修改" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator(".memory-row")).toContainText("夏夏");
  await page.getByRole("link", { name: "模型与能力" }).click();
  await page.getByLabel("API Key", { exact: true }).fill("private-test-input");
  expect(
    await page.evaluate(() => JSON.stringify(sessionStorage)),
  ).not.toContain("private-test-input");
  await page.getByRole("link", { name: "系统设置" }).click();
  await page.getByRole("button", { name: "高级参数与组件" }).click();
  await page.getByLabel("最多连续工具调用", { exact: false }).fill("20");
  await page
    .locator(".page:not([hidden])")
    .getByRole("button", { name: "应用设置", exact: true })
    .click();
  await expect(page.locator(".page:not([hidden]) .saved-inline")).toHaveText(
    "已保存",
  );
  await page.getByRole("link", { name: "模型与能力" }).click();
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue(
    "private-test-input",
  );
});
test("mobile layout, keyboard navigation and unsaved conflict remain usable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("link", { name: "群聊与人格" }).click();
  await expect(page.getByLabel("试聊消息")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../.local/console-browser-results/groups-mobile.png",
    fullPage: true,
  });
  await page
    .getByLabel("性格与交流习惯", { exact: false })
    .fill("保留这段未保存内容");
  await page.route("**/api/proactive", async (route) => {
    if (route.request().method() === "GET") {
      const response = await route.fetch();
      const value = await response.json();
      value.config.revision = "f".repeat(32);
      await route.fulfill({ json: value });
    } else await route.continue();
  });
  await page.getByRole("button", { name: "刷新群聊" }).click();
  await expect(
    page.getByRole("button", { name: "应用设置", exact: true }),
  ).toBeDisabled();
  await expect(page.getByLabel("性格与交流习惯", { exact: false })).toHaveValue(
    "保留这段未保存内容",
  );
});

test("reasoning settings save shared roles, supported choices and reload without touching tool limits", async ({
  page,
}) => {
  await page.getByRole("link", { name: "模型与能力" }).click();
  await page.getByRole("button", { name: "推理等级", exact: true }).click();
  const chat = page.getByLabel("聊天推理等级", { exact: true });
  const shared = page.getByLabel("参与判断、内容复核推理等级", { exact: true });
  const before = (await (await page.request.get("/api/runtime")).json()).config;
  await expect(chat).toHaveValue("low");
  await expect(chat.locator('option[value="disabled"]')).toHaveCount(0);
  await expect(shared.locator('option[value="max"]')).toHaveCount(0);
  await chat.selectOption("high");
  await shared.selectOption("high");
  const request = page.waitForRequest(
    (r) => r.url().endsWith("/api/runtime") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  const payload = (await request).postDataJSON();
  expect(payload.reasoning).toEqual({
    chat: "high",
    decision: "high",
    reconcile: "high",
  });
  expect(payload.tool_iterations).toBe(before.tool_iterations);
  expect(payload.request_timeout).toBe(before.request_timeout);
  await expect(page.getByRole("status")).toContainText(
    "推理等级已生效，将用于新消息",
  );
  await page.reload();
  await page.getByRole("button", { name: "推理等级", exact: true }).click();
  await expect(chat).toHaveValue("high");
  await expect(shared).toHaveValue("high");
  await page.screenshot({
    path: "../.local/console-browser-results/reasoning-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../.local/console-browser-results/reasoning-mobile.png",
    fullPage: true,
  });
});

test("native GIF defaults, settings and playback duration survive saving and reload", async ({
  page,
}) => {
  await page.getByRole("link", { name: "模型与能力" }).click();
  await page.getByRole("button", { name: "工具与上下文", exact: true }).click();
  await expect(page.getByLabel("GIF 默认帧数", { exact: true })).toHaveValue(
    "16",
  );
  await expect(page.getByLabel("原生 GIF 模型", { exact: true })).toHaveValue(
    "gpt-image-2.6-sunburst",
  );
  await page
    .getByLabel("原生 GIF 模型", { exact: true })
    .fill("gpt-image-2.6-flare");
  await page
    .getByLabel("GIF 每帧尺寸", { exact: true })
    .selectOption("512x512");
  await page.getByLabel("GIF 每帧时长（毫秒）", { exact: true }).fill("100");
  await page.getByLabel("图片复杂度", { exact: true }).selectOption("medium");
  await page.getByLabel("GIF 服务", { exact: true }).selectOption("official");
  await expect(
    page.getByLabel("GIF 独立服务地址", { exact: true }),
  ).toHaveValue("https://api.openai.com/v1");
  await page.getByLabel("API Key", { exact: true }).fill("synthetic-gif-key");
  expect(
    await page.evaluate(() => JSON.stringify(sessionStorage)),
  ).not.toContain("synthetic-gif-key");
  await expect(
    page.getByText("当前设置每轮约 1.60 秒。", { exact: true }),
  ).toBeVisible();
  const submitted = page.waitForRequest(
    (r) => r.url().endsWith("/api/configure") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  expect((await submitted).postDataJSON()).toMatchObject({
    gif_model: "gpt-image-2.6-flare",
    gif_size: "512x512",
    gif_frame_duration_ms: 100,
    image_complexity: "medium",
    gif_provider: "official",
    gif_base_url: "https://api.openai.com/v1",
    gif_api_key: "synthetic-gif-key",
  });
  await expect(page.locator(".save-bar")).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: "工具与上下文", exact: true }).click();
  await expect(
    page.getByLabel("GIF 独立服务地址", { exact: true }),
  ).toHaveValue("https://api.openai.com/v1");
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("");
  await expect(page.getByPlaceholder("已保存 · 留空保留")).toBeVisible();
  await page.getByLabel("GIF 服务", { exact: true }).selectOption("image");
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  await expect(page.locator(".save-bar")).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: "工具与上下文", exact: true }).click();
  await expect(page.getByLabel("GIF 服务", { exact: true })).toHaveValue(
    "image",
  );
  await page.getByLabel("GIF 服务", { exact: true }).selectOption("official");
  await expect(page.getByPlaceholder("已保存 · 留空保留")).toBeVisible();
  await expect(page.getByLabel("原生 GIF 模型", { exact: true })).toHaveValue(
    "gpt-image-2.6-flare",
  );
  await expect(page.getByLabel("GIF 默认帧数", { exact: true })).toHaveValue(
    "16",
  );
  await expect(
    page.getByLabel("GIF 每帧时长（毫秒）", { exact: true }),
  ).toHaveValue("100");
  await page.screenshot({
    path: "../.local/console-browser-results/native-gif-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByLabel("原生 GIF 模型", { exact: true })
    .scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../.local/console-browser-results/native-gif-mobile.png",
    fullPage: true,
  });
});

test("reasoning draft survives navigation and conflicting saves keep the draft", async ({
  page,
}) => {
  await page.getByRole("link", { name: "模型与能力" }).click();
  await page.getByRole("button", { name: "推理等级", exact: true }).click();
  const chat = page.getByLabel("聊天推理等级", { exact: true });
  await chat.selectOption("medium");
  await page.getByRole("button", { name: "模型分工", exact: true }).click();
  await page.getByRole("button", { name: "推理等级", exact: true }).click();
  await expect(chat).toHaveValue("medium");
  await page.route("**/api/runtime", async (route) => {
    if (route.request().method() === "POST")
      await route.fulfill({
        status: 409,
        json: { error: "高级参数已在其他页面变化，请重新载入后编辑。" },
      });
    else await route.continue();
  });
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  await expect(
    page.getByText("高级参数已在其他页面变化，请重新载入后编辑。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(chat).toHaveValue("medium");
  await expect(
    page.getByText("推理等级已生效，将用于新消息", { exact: true }),
  ).toHaveCount(0);
});
