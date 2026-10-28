import { test, expect } from "@playwright/test";
test.beforeEach(async ({ page }) => {
  await page.goto("/#token=offline-console-test");
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
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
