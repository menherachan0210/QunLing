const $ = (id) => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || sessionStorage.getItem("qqbot-setup") || "";
if (fragment.has("token")) history.replaceState(null, "", location.pathname);
if (token) sessionStorage.setItem("qqbot-setup", token);
let loaded = false;
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) $("unlock").hidden = false;
    throw new Error(value.error || "请求暂未完成");
  }
  return value;
}
async function poll() {
  try {
    const state = await api("/api/status");
    $("unlock").hidden = true;
    $("status").textContent = state.message;
    $("save").disabled = !state.plugins_ready || state.stage === "configuring";
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
  } catch (error) { $("status").textContent = error.message; }
}
$("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
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
