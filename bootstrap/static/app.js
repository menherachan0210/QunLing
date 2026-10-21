const $ = (id) => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || sessionStorage.getItem("qqbot-setup") || "";
if (fragment.has("token")) history.replaceState(null, "", location.pathname + location.search);
if (token) sessionStorage.setItem("qqbot-setup", token);
let loaded = false;
let authenticated = false;
let authEpoch = 0;
let setupState = null;
let updateState = null;
let updateAction = false;
let configAction = false;
let qrAction = false;
let adminAction = false;
let updateExpected = sessionStorage.getItem("qunling-update-pending") || "";
let polling = false;
let pollPromise = null;
const terminalPhases = new Set(["completed", "rolled_back", "failed"]);
const phaseNames = {
  idle: "已是最新", checking: "检查中", available: "发现更新", updating: "更新中", queued: "等待更新",
  backing_up: "备份中", building: "构建中", restarting: "重启中", verifying: "验证中",
  completed: "更新完成", rolling_back: "回滚中", rolled_back: "已回滚", failed: "更新失败",
  recovery_required: "需要恢复", unsupported: "未启用", unavailable: "等待连接"
};
function tone(id, value) { $(id).dataset.tone = value; }
function connectionTone(value) {
  for (const id of ["global-state", "sidebar-status"]) {
    tone(id, value);
    if ($(id).parentElement) $(id).parentElement.dataset.tone = value;
  }
}
function text(id, value) { $(id).textContent = value; }
function hideAdmin() {
  text("admin-info", "");
  $("admin-info").hidden = true;
  text("admin", "查看管理账号");
  $("admin").setAttribute("aria-expanded", "false");
}
function locked(message) {
  authEpoch += 1;
  authenticated = false;
  setupState = null;
  $("console-content").hidden = true;
  $("unlock").hidden = false;
  $("qr").hidden = true;
  $("qr").removeAttribute("src");
  $("ready").hidden = true;
  text("status", message || "使用专属设置链接，或输入设置令牌以连接控制台。");
  tone("status", "neutral");
  text("sidebar-status", "等待连接");
  connectionTone("neutral");
  hideAdmin();
  controls();
}
async function api(path, body, timeout = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal
    });
    const value = await response.json();
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) locked(value.error);
      const error = new Error(value.error || "请求暂未完成");
      error.status = response.status;
      throw error;
    }
    return value;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("连接超时，正在等待服务恢复。");
    throw error;
  } finally { clearTimeout(timer); }
}
function controls() {
  const updating = updateAction || Boolean(updateExpected) || Boolean(updateState?.busy);
  $("save").disabled = !authenticated || configAction || updating || !setupState?.plugins_ready || setupState.stage === "configuring";
  text("save-label", configAction || setupState?.stage === "configuring" ? "正在应用配置…" : "保存并启用");
  $("check-update").disabled = !authenticated || updating || !updateState?.available;
  $("apply-update").disabled = !authenticated || configAction || updating || !updateState?.available || !updateState.update_available || !updateState.latest_revision || !setupState || ["starting", "configuring"].includes(setupState.stage);
  $("refresh").disabled = !authenticated || qrAction || updating || !setupState || setupState.qq_logged_in;
  $("admin").disabled = !authenticated || adminAction;
}
function summary(name, label, detail, state = "neutral") {
  text("summary-" + name, label);
  text("summary-" + name + "-detail", detail);
  $("summary-" + name).closest(".summary-card").dataset.tone = state;
}
function renderSetup(state) {
  const ready = state.stage === "ready" && state.configured && state.qq_connected;
  const failed = state.stage === "error";
  const label = failed ? "需要处理" : ready ? "运行就绪" : state.stage === "configuring" ? "配置中" : !state.plugins_ready ? "初始化中" : !state.configured ? "等待配置" : "等待 QQ 登录";
  text("global-state", label);
  text("sidebar-status", label);
  text("status", state.message || "管理服务已连接。");
  tone("status", failed ? "error" : ready ? "success" : "neutral");
  connectionTone(failed ? "error" : ready ? "success" : "warning");
  text("last-sync", "同步于 " + new Date().toLocaleTimeString("zh-CN", {hour12: false}));
  summary("qq", state.qq_connected ? "已连接" : state.qq_logged_in ? "连接中" : "等待登录",
    state.qq_connected ? "消息通道正常连接" : state.qq_logged_in ? "QQ 已登录，等待机器人连接" : "使用手机 QQ 扫码登录", state.qq_connected ? "success" : "warning");
  summary("ai", state.configured ? "已配置" : "待配置", state.settings?.chat_model || "填写接口地址与模型", state.configured ? "success" : "neutral");
  summary("plugins", state.plugins_ready ? "已就绪" : "初始化中", state.plugins_ready ? "功能组件已完成初始化" : "首次安装可能需要几分钟", state.plugins_ready ? "success" : "warning");
  $("ready").hidden = !ready;
  const showQR = Boolean(state.qr_image) && !state.qq_logged_in;
  $("qr").hidden = !showQR;
  $("connection-art").hidden = showQR;
  $("connection-card").dataset.state = state.qq_connected ? "connected" : state.qq_logged_in ? "pending" : "waiting";
  if (showQR && $("qr").src !== state.qr_image) $("qr").src = state.qr_image;
  if (!showQR) $("qr").removeAttribute("src");
  text("qq-status", state.qq_connected ? "QQ 已连接，随时可以开始聊" : state.qq_logged_in ? "QQ 已登录，正在等待机器人连接" : showQR ? "使用手机 QQ 扫码，并在手机上确认" : "正在等待登录二维码…");
  if (!loaded && state.settings?.base_url) {
    for (const [key, value] of Object.entries(state.settings)) {
      if (key !== "api_key" && $("settings").elements[key]) {
        const inheritsChat = (key === "vision_model" || key === "search_model") && value === state.settings.chat_model;
        $("settings").elements[key].value = inheritsChat ? "" : value;
      }
    }
    $("settings").elements.api_key.placeholder = "已保存 · 留空保留原密钥";
    loaded = true;
  }
}
function renderCleanup(value) {
  if (!value) {
    text("cleanup-status", "暂无清理记录");
    text("cleanup-detail", "更新器空闲时整理历史版本，保留必要的回滚资料。");
    text("cleanup-time", "等待更新器返回维护记录");
    return;
  }
  const names = {completed: "自动维护正常", partial: "已保留受保护的历史", failed: "清理需要检查", skipped: "暂未执行清理"};
  text("cleanup-status", names[value.status] || "等待维护记录");
  const count = Number.isInteger(value.keep_backups) ? `保留最近 ${value.keep_backups} 次成功更新的备份` : "保留必要的回滚资料";
  const hasCounts = [value.removed_jobs, value.removed_images].every(Number.isInteger);
  const removed = hasCounts ? `；本次清理 ${value.removed_jobs} 份历史备份、${value.removed_images} 个镜像` : "";
  const needsReview = value.status === "failed" || (value.warnings || []).some((code) => code !== "unmanaged_or_unresolved_history_preserved");
  text("cleanup-detail", count + removed + (needsReview ? "。部分项目暂未清理，请查看更新器维护记录。" : "。运行中的服务与回滚依赖会保留。"));
  const date = new Date(value.checked_at);
  text("cleanup-time", Number.isNaN(date.getTime()) ? "暂无检查时间" : "最近检查 · " + date.toLocaleString("zh-CN", {hour12: false}));
}
function renderUpdate(value, authoritative = false) {
  updateState = value;
  // Only recovered status for this exact target releases the update latch.
  // Accepted POSTs, stale completions and disconnections do not prove success.
  const finishedTarget = value.phase === "completed" ? value.current_revision === updateExpected : value.latest_revision === updateExpected;
  if (authoritative && value.available && !value.busy && terminalPhases.has(value.phase) && finishedTarget) {
    updateExpected = "";
    sessionStorage.removeItem("qunling-update-pending");
  }
  text("current-revision", value.current_revision?.slice(0, 12) || "本地构建");
  text("latest-revision", value.latest_revision?.slice(0, 12) || "尚未检查");
  $("current-revision").title = value.current_revision || "此构建未记录 Git 提交";
  $("latest-revision").title = value.latest_revision || "";
  text("update-source", value.repository ? `${value.repository} · ${value.branch || "默认分支"}` : "跟随此部署配置的项目仓库");
  const confirming = updateExpected && terminalPhases.has(value.phase);
  const phaseLabel = confirming ? "等待确认" : value.phase === "idle" && !value.latest_revision ? "尚未检查" : phaseNames[value.phase] || (value.busy ? "处理中" : "等待检查");
  const failed = ["failed", "recovery_required"].includes(value.phase);
  const stateTone = failed ? "error" : value.busy || value.update_available || confirming ? "warning" : value.available && value.latest_revision ? "success" : "neutral";
  text("update-phase", phaseLabel);
  tone("update-phase", stateTone);
  summary("update", phaseLabel, value.busy ? "任务进行中，状态会自动同步" : value.latest_revision ? "当前 " + (value.current_revision?.slice(0, 8) || "本地构建") : "检查仓库中的新版本", stateTone);
  text("update-message", updateExpected && !value.available ? "更新期间连接暂时中断，正在等待服务恢复；尚未确认更新完成。" : confirming ? "正在确认本次更新结果，请稍候。" : value.message || "点击检查更新，获取仓库中的最新版本。");
  $("update-error").hidden = !value.error;
  text("update-error", value.error || "");
  const steps = {queued: 0, building: 0, updating: 0, backing_up: 1, restarting: 2, verifying: 3};
  const step = steps[value.phase];
  $("update-progress").hidden = !value.busy || step === undefined;
  document.querySelectorAll("[data-update-step]").forEach((element, index) => {
    element.classList.toggle("is-current", index === step);
    element.classList.toggle("is-done", index < step);
  });
  renderCleanup(value.cleanup);
  controls();
}
async function pollUpdate(epoch = authEpoch) {
  try {
    const value = await api("/api/update");
    if (epoch === authEpoch) renderUpdate(value, true);
  } catch (error) {
    updateState = {...(updateState || {}), available: false};
    text("update-phase", "等待连接");
    tone("update-phase", "neutral");
    summary("update", "等待连接", "更新服务暂时无法连接");
    text("update-message", updateExpected ? "更新期间连接暂时中断，正在等待服务恢复；尚未确认更新完成。" : "暂时无法读取版本状态，正在自动重试。");
    controls();
  }
}
async function pollSetup(epoch = authEpoch) {
  try {
    const state = await api("/api/status");
    if (epoch !== authEpoch) return;
    setupState = state;
    authenticated = true;
    $("unlock").hidden = true;
    $("console-content").hidden = false;
    renderSetup(state);
  } catch (error) {
    setupState = null;
    text("status", updateExpected ? "正在等待更新后的管理服务恢复…" : error.message);
    tone("status", "warning");
    text("global-state", "连接中断");
    text("sidebar-status", "等待重连");
    connectionTone("warning");
    summary("qq", "状态未知", "管理服务恢复后会自动同步", "warning");
    summary("ai", "等待同步", "正在重新连接管理服务");
    summary("plugins", "等待同步", "正在重新连接管理服务");
    $("ready").hidden = true;
    $("qr").hidden = true;
    $("qr").removeAttribute("src");
    $("connection-art").hidden = false;
    $("connection-card").dataset.state = "waiting";
    text("qq-status", "管理服务暂时断开，正在重新确认连接状态。");
  }
  controls();
}
async function poll(fresh = false) {
  if (polling) {
    await pollPromise;
    // A mutation needs a request started after its POST completed. Waiting for
    // an older poll alone could apply a stale ready state and unlock controls.
    if (fresh === true) return poll(true);
    return;
  }
  if (!token) { locked(); return; }
  polling = true;
  const epoch = authEpoch;
  $("refresh-state").classList.add("is-loading");
  $("refresh-state").disabled = true;
  pollPromise = Promise.allSettled([pollSetup(epoch), pollUpdate(epoch)]).finally(() => {
    polling = false;
    pollPromise = null;
    $("refresh-state").classList.remove("is-loading");
    $("refresh-state").disabled = false;
  });
  return pollPromise;
}
$("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("save").disabled) return;
  const form = event.currentTarget;
  text("form-message", "");
  configAction = true; controls();
  try {
    await api("/api/configure", Object.fromEntries(new FormData(form)));
    form.elements.api_key.value = "";
    form.elements.api_key.type = "password";
    $("show-key").setAttribute("aria-pressed", "false");
    $("show-key").setAttribute("aria-label", "显示 API Key");
    $("show-key").title = "显示 API Key";
    text("form-message", "配置已提交，正在应用到机器人。");
    tone("form-message", "success");
  } catch (error) {
    text("form-message", error.message);
    tone("form-message", "error");
  } finally { await poll(true); configAction = false; controls(); }
});
$("show-key").addEventListener("click", () => {
  const input = $("settings").elements.api_key;
  const reveal = input.type === "password";
  input.type = reveal ? "text" : "password";
  $("show-key").setAttribute("aria-pressed", String(reveal));
  $("show-key").setAttribute("aria-label", reveal ? "隐藏 API Key" : "显示 API Key");
  $("show-key").title = reveal ? "隐藏 API Key" : "显示 API Key";
});
$("check-update").addEventListener("click", async () => {
  if ($("check-update").disabled) return;
  updateAction = true; controls();
  text("update-message", "正在检查 GitHub 上的新版本…");
  try { renderUpdate(await api("/api/update/check", {}, 45000)); }
  catch (error) { text("update-message", error.message); }
  finally { updateAction = false; controls(); }
});
$("apply-update").addEventListener("click", async () => {
  if ($("apply-update").disabled) return;
  const revision = updateState.latest_revision;
  if (!window.confirm(`更新到 ${revision.slice(0, 12)}？\n\n切换版本会短暂停机并中断正在进行的任务，请在空闲时更新。更新前会备份配置和数据，验证失败时尝试回滚。请等待页面恢复并确认结果。`)) return;
  updateAction = true;
  updateExpected = revision;
  sessionStorage.setItem("qunling-update-pending", revision);
  controls();
  try { renderUpdate(await api("/api/update/apply", {revision})); }
  catch (error) {
    if (error.status && error.status < 500) {
      updateExpected = "";
      sessionStorage.removeItem("qunling-update-pending");
    }
    text("update-message", `${error.message} 正在重新确认更新状态，请勿重复提交。`);
  } finally { updateAction = false; await pollUpdate(); controls(); }
});
$("refresh").addEventListener("click", async () => {
  if ($("refresh").disabled) return;
  qrAction = true; controls();
  try { await api("/api/qr/refresh", {}); await poll(); }
  catch (error) { text("qq-status", error.message); }
  finally { qrAction = false; controls(); }
});
$("admin").addEventListener("click", async () => {
  if ($("admin").disabled) return;
  if (!$("admin-info").hidden) { hideAdmin(); return; }
  adminAction = true; controls();
  const epoch = authEpoch;
  try {
    const value = await api("/api/admin");
    if (!authenticated || epoch !== authEpoch) return;
    text("admin-info", value.user ? `账号：${value.user}\n密码：${value.password}` : "管理账号正在初始化");
    $("admin-info").hidden = false;
    text("admin", "隐藏管理账号");
    $("admin").setAttribute("aria-expanded", "true");
  } catch (error) {
    if (authenticated) { text("admin-info", error.message); $("admin-info").hidden = false; }
  } finally { adminAction = false; controls(); }
});
async function unlock() {
  const value = $("token").value.trim();
  if (!value || polling) return;
  token = value;
  $("token").value = "";
  loaded = false;
  sessionStorage.setItem("qqbot-setup", token);
  $("unlock-button").disabled = true;
  try { await poll(); }
  finally { $("unlock-button").disabled = false; }
}
$("unlock-button").addEventListener("click", unlock);
$("token").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); unlock(); }
});
$("refresh-state").addEventListener("click", poll);
const navLinks = [...document.querySelectorAll(".nav-link")];
function selectNav(id) {
  navLinks.forEach((link) => {
    const active = link.getAttribute("href") === "#" + id;
    link.classList.toggle("is-active", active);
    if (active) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
}
navLinks.forEach((link) => link.addEventListener("click", () => selectNav(link.getAttribute("href").slice(1))));
if (typeof IntersectionObserver !== "undefined") {
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting);
    if (visible.length) selectNav(visible[0].target.id);
  }, {rootMargin: "-10% 0px -65% 0px"});
  ["overview", "models", "connection", "maintenance", "advanced"].forEach((id) => observer.observe($(id)));
}
poll();
setInterval(poll, 4000);
