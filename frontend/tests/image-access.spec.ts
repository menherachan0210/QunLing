import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/#token=offline-console-test");
  await page.getByRole("link", { name: "模型与能力" }).click();
  await page.getByRole("button", { name: "生图权限", exact: true }).click();
});

test("image ban persists, respects scope, keeps other settings and can be removed", async ({
  page,
}) => {
  const before = (await (await page.request.get("/api/proactive")).json())
    .config;
  await page.getByRole("button", { name: "添加成员" }).click();
  await page.getByLabel("成员 QQ 号", { exact: true }).fill("10001");
  await page.getByLabel("备注", { exact: false }).fill("测试成员");
  await expect(page.getByLabel("生效范围", { exact: true })).toHaveValue("");
  await expect(page.getByLabel("拦截时怎么回复", { exact: true })).toHaveValue(
    "persona",
  );
  await page.getByLabel("生效范围", { exact: true }).selectOption("30001");
  await page.getByLabel("拦截时怎么回复", { exact: true }).selectOption("text");
  await page
    .getByLabel("专属回复", { exact: false })
    .fill("生图资格都没了，还搁这儿点菜呢。");
  const submitted = page.waitForRequest(
    (r) => r.url().endsWith("/api/proactive") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  const payload = (await submitted).postDataJSON();
  expect(payload.image_access.blocked_members).toEqual([
    {
      user_id: "10001",
      group_id: "30001",
      note: "测试成员",
      reply_mode: "text",
      reply_text: "生图资格都没了，还搁这儿点菜呢。",
    },
  ]);
  expect(payload.groups).toEqual(before.groups);
  expect(payload.enabled).toBe(before.enabled);
  await expect(page.locator(".save-bar")).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: "生图权限", exact: true }).click();
  await expect(page.getByLabel("成员 QQ 号", { exact: true })).toHaveValue(
    "10001",
  );
  await expect(page.getByLabel("生效范围", { exact: true })).toHaveValue(
    "30001",
  );
  await expect(page.getByLabel("专属回复", { exact: false })).toHaveValue(
    "生图资格都没了，还搁这儿点菜呢。",
  );
  await page.screenshot({
    path: "../.local/console-browser-results/image-access-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../.local/console-browser-results/image-access-mobile.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "移除成员规则 1", exact: true })
    .click();
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  await expect(page.locator(".save-bar")).toHaveCount(0);
  expect(
    (await (await page.request.get("/api/proactive")).json()).config
      .image_access.blocked_members,
  ).toEqual([]);
});

test("invalid rules and conflicting saves retain the blacklist draft", async ({
  page,
}) => {
  await page.getByRole("button", { name: "添加成员" }).click();
  await page.getByLabel("成员 QQ 号", { exact: true }).fill("不是QQ号");
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  await expect(
    page.getByText("请填写有效的成员 QQ 号。", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("成员 QQ 号", { exact: true }).fill("10002");
  await page.getByLabel("拦截时怎么回复", { exact: true }).selectOption("text");
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  await expect(
    page.getByText("选择固定台词的成员需要填写回复内容。", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("专属回复", { exact: false }).fill("还想点菜呢？");
  await page.route("**/api/proactive", async (route) => {
    if (route.request().method() === "POST")
      await route.fulfill({
        status: 409,
        json: { error: "设置已在其他页面更改，请刷新后编辑。" },
      });
    else await route.continue();
  });
  await page.getByRole("button", { name: "应用设置", exact: true }).click();
  await expect(
    page.getByText("设置已在其他页面更改，请刷新后编辑。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("专属回复", { exact: false })).toHaveValue(
    "还想点菜呢？",
  );
  await page.getByRole("button", { name: "模型分工", exact: true }).click();
  await page.getByRole("button", { name: "生图权限", exact: true }).click();
  await expect(page.getByLabel("成员 QQ 号", { exact: true })).toHaveValue(
    "10002",
  );
});
