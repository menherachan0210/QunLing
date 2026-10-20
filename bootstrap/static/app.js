const $ = (id) => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || sessionStorage.getItem("qqbot-setup") || "";
if (fragment.has("token")) history.replaceState(null, "", location.pathname);
if (token) sessionStorage.setItem("qqbot-setup", token);
let loaded = false;
let setupState = null;
let updateState = null;
let updateAction = false;
let updateExpected = sessionStorage.getItem("qunling-update-pending") || "";
let polling = false;
const terminalPhases = new Set(["completed", "rolled_back", "failed"]);
const phaseNames = {
  idle: "等待检查", checking: "检查中", available: "发现更新", updating: "更新中", queued: "等待更新",
  backing_up: "备份中", building: "构建中", restarting: "重启中", verifying: "验证中",
  completed: "更新完成", rolling_back: "回滚中", rolled_back: "已回滚", failed: "更新失败",
  recovery_required: "需要恢复",
  unsupported: "未启用", unavailable: "等待连接"
};
async function api(path, body, timeout = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal
    });
    const value = await response.json();
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) $("unlock").hidden = false;
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
  $("save").disabled = updating || !setupState?.plugins_ready || setupState.stage === "configuring";
  $("check-update").disabled = updating || !updateState?.available;
  $("apply-update").disabled = updating || !updateState?.available || !updateState.update_available || !updateState.latest_revision || !setupState || ["starting", "configuring"].includes(setupState.stage);
}
function renderUpdate(value, authoritative = false) {
  updateState = value;
  // A POST response or a disappearing connection is never evidence that an
  // update finished. Only the recovered status endpoint can clear this latch.
  const finishedTarget = value.phase === "completed" ? value.current_revision === updateExpected : value.latest_revision === updateExpected;
  if (authoritative && value.available && !value.busy && terminalPhases.has(value.phase) && finishedTarget) {
    updateExpected = "";
    sessionStorage.removeItem("qunling-update-pending");
  }
  $("current-revision").textContent = value.current_revision?.slice(0, 12) || "本地构建（未记录提交）";
  $("latest-revision").textContent = value.latest_revision?.slice(0, 12) || "尚未检查";
  $("current-revision").title = value.current_revision || "";
  $("latest-revision").title = value.latest_revision || "";
  $("update-source").textContent = value.repository ? `${value.repository} · ${value.branch || "默认分支"}` : "跟随此部署配置的项目仓库";
  const confirming = updateExpected && terminalPhases.has(value.phase);
  $("update-phase").textContent = confirming ? "等待确认" : phaseNames[value.phase] || (value.busy ? "处理中" : "等待检查");
  $("update-message").textContent = updateExpected && !value.available ? "更新期间连接暂时中断，正在等待服务恢复；尚未确认更新完成。" : confirming ? "正在确认本次更新结果，请稍候。" : value.message;
  $("update-error").hidden = !value.error;
  $("update-error").textContent = value.error || "";
  controls();
}
async function pollUpdate() {
  try { renderUpdate(await api("/api/update"), true); }
  catch (error) {
    updateState = {...(updateState || {}), available: false};
    $("update-phase").textContent = "等待连接";
    $("update-message").textContent = updateExpected ? "更新期间连接暂时中断，正在等待服务恢复；尚未确认更新完成。" : "暂时无法读取版本状态，请稍后重试。";
    controls();
  }
}
async function pollSetup() {
  try {
    const state = await api("/api/status");
    setupState = state;
    $("unlock").hidden = true;
    $("status").textContent = state.message;
    $("ready").hidden = !(state.configured && state.qq_connected);
    $("qr").hidden = !state.qr_image || state.qq_logged_in;
    if (state.qr_image && $("qr").src !== state.qr_image) $("qr").src = state.qr_image;
    $("qq-status").textContent = state.qq_connected ? "QQ 已登录并连接机器人" : state.qq_logged_in ? "QQ 已登录，等待 AI 配置完成" : "请使用手机 QQ 扫描二维码并确认登录";
    $("refresh").disabled = state.qq_logged_in;
    if (!loaded && state.settings.base_url) {
      for (const [key, value] of Object.entries(state.settings)) {
        if (key !== "api_key" && $("settings").elements[key]) {
          const inheritsChat = (key === "vision_model" || key === "search_model") && value === state.settings.chat_model;
          $("settings").elements[key].value = inheritsChat ? "" : value;
        }
      }
      $("settings").elements.api_key.placeholder = "已保存，留空保留原密钥";
      loaded = true;
    }
  } catch (error) {
    setupState = null;
    $("status").textContent = updateExpected ? "正在等待更新后的管理服务恢复…" : error.message;
  }
  controls();
}
async function poll() {
  if (polling) return;
  polling = true;
  try { await Promise.allSettled([pollSetup(), pollUpdate()]); }
  finally { polling = false; }
}
$("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("save").disabled) return;
  const form = event.currentTarget;
  $("form-message").textContent = "";
  $("save").disabled = true;
  try {
    await api("/api/configure", Object.fromEntries(new FormData(form)));
    form.elements.api_key.value = "";
    $("form-message").textContent = "已提交，正在应用配置。";
    await poll();
  } catch (error) { $("form-message").textContent = error.message; await poll(); }
});
$("check-update").addEventListener("click", async () => {
  if ($("check-update").disabled) return;
  updateAction = true; controls();
  $("update-message").textContent = "正在检查 GitHub 上的新版本…";
  try { renderUpdate(await api("/api/update/check", {}, 45000)); }
  catch (error) { $("update-message").textContent = error.message; }
  finally { updateAction = false; controls(); }
});
$("apply-update").addEventListener("click", async () => {
  if ($("apply-update").disabled) return;
  const revision = updateState.latest_revision;
  if (!window.confirm(`更新到 ${revision.slice(0, 12)}？\n\n切换版本会短暂停机并中断正在进行的任务，建议空闲时更新。更新前自动备份配置和数据，验证失败时尝试回滚。请等待页面恢复并确认结果。`)) return;
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
    $("update-message").textContent = `${error.message} 正在重新确认更新状态，请勿重复提交。`;
  } finally { updateAction = false; await pollUpdate(); controls(); }
});
$("refresh").addEventListener("click", async () => {
  try { await api("/api/qr/refresh", {}); await poll(); }
  catch (error) { $("qq-status").textContent = error.message; }
});
$("admin").addEventListener("click", async () => {
  try {
    const value = await api("/api/admin");
    $("admin-info").textContent = value.user ? `账号：${value.user}\n密码：${value.password}` : "管理账号正在初始化";
  } catch (error) { $("admin-info").textContent = error.message; }
});
$("unlock-button").addEventListener("click", () => {
  token = $("token").value.trim(); $("token").value = "";
  sessionStorage.setItem("qqbot-setup", token); poll();
});
poll(); setInterval(poll, 4000);
