const $ = (id) => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || sessionStorage.getItem("qqbot-setup") || "";
if (fragment.has("token")) history.replaceState(null, "", location.pathname + location.search);
if (token) sessionStorage.setItem("qqbot-setup", token);
let loaded = false;
let authenticated = false;
let resetKnowledge = () => {};
let resetCapabilities = () => {};
let authEpoch = 0;
let setupState = null;
let qqStatusReceivedAt = 0;
let updateState = null;
let updateAction = false;
let configAction = false;
let qrAction = false;
let adminAction = false;
let proactiveDraft = null;
let proactiveGroups = [];
let proactiveSelected = "";
let proactiveDirty = false;
let proactiveConflict = false;
let proactiveConfigReady = false;
let proactiveGroupsReady = false;
let proactiveAction = false;
let proactiveLoading = false;
let proactiveStatus = null;
let proactiveSavedConfig = null;
let proactiveBuiltinStyle = null;
let proactiveLastPoll = 0;
const proactiveModes = {off: "仅 @ 回复", quiet: "安静参与", natural: "自然参与", active: "活跃参与"};
const proactiveModeNotes = {
  off: "保留原来的 @ 和 /ai 回复方式，不判断是否主动插话。",
  quiet: "回应直接称呼、引用或对自己的追问，不主动加入其他人的话题。",
  natural: "自然接续对话，也会选择性加入讨论；普通消息和表情不会打断已开始的回复。",
  active: "更积极地接话和参与讨论，发言间隔更短、额度更高；仍会合并消息，避免逐条刷屏。"
};
const proactiveRuntimeStates = {
  off: ["未开启", "neutral", "此群当前不参与自由对话，@ 和 /ai 仍可正常使用。"],
  quiet_hours: ["安静时段", "neutral", "正在执行已保存的安静时段，暂不主动发言。"],
  explicit_busy: ["有明确任务进行中", "neutral", "正在处理明确提问；同一个人的后续消息按顺序等待，其他人的对话可以继续。"],
  pending: ["正在接话", "neutral", "按说话人分别处理对话，同一个人的连续消息会合并等待，不打断当前回复；不同人的提问和闲聊可以并行推进。发言仍按群控制间隔。"],
  cooldown: ["控制发言频率", "neutral", "正在等待发言间隔，待处理消息会合并排队；点名或引用等接话可能采用更短的间隔。"],
  budget_exhausted: ["已达参与限额", "warning", "本群或机器人的自由对话限额已用完，额度随时间恢复。@ 提问不受此预算影响。"],
  ready: ["已就绪", "success", "正在接收本群新消息，合并后按上下文选择是否接话；并非每条消息都调用模型，也不保证每条都会回复。"],
  storage_error: ["已暂停", "error", "运行状态存储异常，已暂停自由对话。请检查服务日志。"],
  unavailable: ["等待服务", "warning", "暂时无法读取自由对话状态，请刷新重试。"],
  disconnected: ["QQ 未连接", "warning", "QQ 尚未连接，登录并恢复连接后才能接收群消息。"],
  unknown: ["状态待确认", "neutral", "当前服务未提供详细运行状态，请刷新或更新服务后再检查。"]
};
const proactiveEventNames = {
  initial: "等待新消息", observed: "已接收新消息", explicit_started: "开始处理明确提问",
  queued: "后续消息已合并等待", media_observed: "已收到非文字消息，不打断当前回复",
  explicit_finished: "明确提问处理结束", quiet_hours: "安静时段，保持安静",
  wait: "判断暂缓接话", silence: "判断无需接话", cooldown: "等待发言间隔，待处理消息已排队",
  budget_exhausted: "达到调用限额", judging: "正在判断是否接话", generating: "正在组织回复",
  sent: "回复已发送", stale: "本次回复已过期，未发送", cancelled: "任务已取消",
  duplicate_reply: "同话题已回复，省略重复内容",
    handing_off: "续聊需要资料，正在转入查询",
    handed_off: "续聊已进入查询流程，等待实际答复",
    handoff_failed: "续聊转入查询失败，未自动重试",
  batch_expired: "本批次已过期，未继续回复", invalid_decision: "判断结果无效，未发言",
  invalid_generation: "回复内容未通过格式检查，未发言",
  model_error: "模型调用失败，未发言", delivery_failed: "发送未确认，未自动重发",
  config_changed: "设置已变化，旧任务已取消"
};
let updateExpected = sessionStorage.getItem("qunling-update-pending") || "";
let polling = false;
let pollPromise = null;
const terminalPhases = new Set(["completed", "rolled_back", "failed"]);
const phaseNames = {
  idle: "已是最新", checking: "检查中", available: "发现更新", updating: "更新中", queued: "等待更新",
  backing_up: "旧版更新中", stopping: "切换中", building: "准备中", restarting: "重启中", verifying: "验证中",
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
function qqHealth(state) {
  const checkedAt = state?.qq_checked_at;
  const validTime = typeof checkedAt === "number" && Number.isFinite(checkedAt) && checkedAt > 0 && checkedAt <= 253402300799;
  const states = {
    online: ["已连接", "QQ 在线，消息通道正常连接", "success"],
    offline: ["QQ 已离线", "QQ 已离线，恢复登录后才能收发消息", "warning"],
    disconnected: ["消息通道断开", "QQ 已登录，但机器人消息通道尚未连接", "warning"],
    waiting_login: ["等待登录", "使用手机 QQ 扫码，并在手机上确认", "warning"],
    connecting: ["连接中", "QQ 正在连接，等待完成在线状态检查", "warning"],
    unknown: ["状态待确认", "暂时无法确认 QQ 是否在线，正在重新检查", "warning"]
  };
  const fresh = state?.qq_status_fresh === true && validTime;
  let code = fresh && Object.hasOwn(states, state?.qq_state) ? state.qq_state : "unknown";
  if (code === "online" && (state.qq_logged_in !== true || state.qq_connected !== true)) code = "unknown";
  const loggedIn = fresh && state?.qq_logged_in === true && ["online", "connecting", "disconnected"].includes(code);
  const [label, detail, stateTone] = states[code];
  return {code, fresh, loggedIn, connected: code === "online", checkedAt: validTime ? checkedAt : null, label, detail, tone: stateTone};
}
function expireQQStatus(now = Date.now()) {
  // Use elapsed browser time, not a comparison with the server's clock.
  if (!authenticated || !setupState?.qq_status_fresh || now - qqStatusReceivedAt < 30000) return;
  setupState = {...setupState, qq_state: "unknown", qq_status_fresh: false, qq_logged_in: false, qq_connected: false};
  renderSetup(setupState);
  renderProactiveRuntime();
  controls();
}
function hideAdmin() {
  text("admin-info", "");
  $("admin-info").hidden = true;
  text("admin", "查看管理账号");
  $("admin").setAttribute("aria-expanded", "false");
}
function locked(message) {
  resetKnowledge();
  resetCapabilities();
  authEpoch += 1;
  clearRequests();
  authenticated = false;
  setupState = null;
  proactiveConfigReady = false;
  proactiveGroupsReady = false;
  proactiveLastPoll = 0;
  proactiveStatus = null;
  proactiveSavedConfig = null;
  proactiveBuiltinStyle = null;
  $("proactive-default-style").value = "";
  $("proactive-runtime").hidden = true;
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
  $("save").disabled = !authenticated || configAction || proactiveAction || updating || !setupState || ["starting", "configuring"].includes(setupState.stage);
  text("save-label", configAction || setupState?.stage === "configuring" ? "正在应用配置…" : "保存所选功能");
  $("check-update").disabled = !authenticated || updating || !updateState?.available;
  $("apply-update").disabled = !authenticated || configAction || proactiveAction || updating || !updateState?.available || !updateState.update_available || !updateState.latest_revision || !setupState || ["starting", "configuring"].includes(setupState.stage);
  $("refresh").disabled = !authenticated || qrAction || updating || !setupState || qqHealth(setupState).loggedIn;
  $("admin").disabled = !authenticated || adminAction;
  const proactiveBlocked = !authenticated || !setupState?.qq_logged_in || configAction || updating || proactiveAction || proactiveLoading || !proactiveConfigReady || !proactiveGroupsReady || proactiveConflict || proactiveStatus?.available === false;
  document.querySelectorAll("[data-proactive-edit], .proactive-group").forEach((element) => { element.disabled = proactiveBlocked; });
  $("proactive-group-settings").disabled = proactiveBlocked || !proactiveSelected;
  $("proactive-default-style").disabled = proactiveBlocked || proactiveBuiltinStyle === null;
  $("proactive-reset-style").disabled = proactiveBlocked || proactiveBuiltinStyle === null;
  $("proactive-reset-group").disabled = proactiveBlocked || !proactiveSelected || !proactiveDraft || !Object.hasOwn(proactiveDraft.groups, proactiveSelected);
  $("proactive-save").disabled = proactiveBlocked || !proactiveDirty;
  $("proactive-refresh").disabled = !authenticated || !setupState?.qq_logged_in || updating || proactiveLoading || proactiveAction;
  $("proactive-search").disabled = !proactiveGroupsReady;
  $("proactive-reload").disabled = !authenticated || updating || proactiveAction || proactiveLoading;
  $("proactive-reload").hidden = !proactiveDirty && !proactiveConflict;
  text("proactive-save-label", proactiveAction ? "正在保存…" : "保存自由对话设置");
}
function proactiveDefaultGroup() {
  return {mode: "off", display_name: "群灵", persona: "", quiet_hours: {enabled: false, start: "23:00", end: "08:00", timezone: "Asia/Shanghai"}};
}
function validProactiveConfig(value) {
  return value?.version === 1 && typeof value.revision === "string" && typeof value.enabled === "boolean" &&
    (value.default_style === undefined || typeof value.default_style === "string") &&
    value.groups && !Array.isArray(value.groups) && typeof value.groups === "object" &&
    Object.entries(value.groups).every(([id, group]) => /^\d+$/.test(id) && group && Object.hasOwn(proactiveModes, group.mode) &&
      typeof group.display_name === "string" && typeof group.persona === "string" && typeof group.quiet_hours?.enabled === "boolean" &&
      typeof group.quiet_hours.start === "string" && typeof group.quiet_hours.end === "string");
}
function proactiveFeedback(message, state = "error") {
  text("proactive-message", message);
  tone("proactive-message", state);
}
function proactiveSummary() {
  if (!proactiveDraft) return;
  const count = Object.values(proactiveDraft.groups).filter((group) => group.mode !== "off").length;
  const paused = !proactiveDraft.enabled;
  text("proactive-summary", count ? `已选择 ${count} 个群${paused ? "，总开关关闭时全部暂停主动发言" : "，其他群保持仅 @ 回复"}。新群不会自动开放。` : "尚未开放任何群。请在下方选择群，并明确设置参与方式。");
  text("proactive-dirty", proactiveDirty ? "有未保存的修改，保存后才会生效" : "设置独立保存，不影响 AI 接口配置");
  text("proactive-state", proactiveDirty ? "待保存" : paused ? "已暂停" : count ? `${count} 个群已开放` : "尚未选群");
  tone("proactive-state", proactiveDirty ? "warning" : !paused && count ? "success" : "neutral");
  renderProactiveRuntime();
}
function proactiveCount(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= 1000000 ? String(value) : "—";
}
function proactiveUsage(used, limit) { return `${proactiveCount(used)} / ${proactiveCount(limit)}`; }
function proactiveTime(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0 || value > 253402300799) return "暂无记录";
  return new Date(value * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false});
}
function renderProactiveRuntime() {
  $("proactive-runtime").hidden = !authenticated || !proactiveSelected;
  if (!authenticated || !proactiveSelected) return;
  const saved = proactiveSavedConfig;
  const status = proactiveStatus;
  const group = status?.groups?.[proactiveSelected];
  const mode = saved?.groups?.[proactiveSelected]?.mode || "off";
  let code = group?.status_code || "unknown";
  if (!proactiveConfigReady || !status) code = "unavailable";
  else if (status.storage_healthy === false) code = "storage_error";
  else if (status.available === false) code = "unavailable";
  else if (!saved?.enabled || mode === "off") code = "off";
  else if (!setupState?.qq_logged_in || !setupState?.qq_connected) code = "disconnected";
  const [label, stateTone, description] = Object.hasOwn(proactiveRuntimeStates, code) ? proactiveRuntimeStates[code] : proactiveRuntimeStates.unknown;
  text("proactive-runtime-state", label);
  tone("proactive-runtime-state", stateTone);
  text("proactive-runtime-note", proactiveDirty ? "以下是服务器已保存设置的状态；当前修改尚未生效。" : "按服务器已保存的设置运行，约每 15 秒刷新。时间为北京时间。");
  let detail = description;
  if (code === "cooldown" && Number.isFinite(group?.cooldown_until) && Number.isFinite(status?.generated_at)) {
    const seconds = Math.ceil(group.cooldown_until - status.generated_at);
    if (seconds > 0 && seconds <= 86400) detail += ` 普通插话还需约 ${seconds < 60 ? `${seconds} 秒` : `${Math.ceil(seconds / 60)} 分钟`}。`;
  }
  const conversations = [group?.active_conversations, group?.queued_conversations, group?.explicit_requests];
  if (!["off", "unavailable", "disconnected", "storage_error", "unknown"].includes(code)
      && conversations.every(value => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= 1000000)) {
    detail += ` 当前 ${conversations[0]} 路接话处理中、${conversations[1]} 路等待、${conversations[2]} 个明确任务。`;
  }
  if (Number.isSafeInteger(group?.handoff_attempts_day) && group.handoff_attempts_day > 0) {
    detail += ` 最近 24 小时有 ${group.handoff_attempts_day} 次续聊转入查询，共用参与额度；入队不代表已回复。`;
  }
  text("proactive-runtime-detail", detail);
  const timings = group?.performance?.stages;
  if (timings && typeof timings === "object") {
    const labels = {judge:"判断", generate:"生成", reconcile:"去重"};
    const parts = Object.entries(labels).flatMap(([key, label]) => {
      const item = timings[key];
      return item && Number.isSafeInteger(item.count) && item.count > 0 && Number.isFinite(item.p50_seconds)
        ? [`${label} ${item.count} 次，中位 ${item.p50_seconds} 秒，P95 ${item.p95_seconds} 秒`] : [];
    });
    if (parts.length) {
      const usage = group?.performance?.usage?.stages;
      const counters = usage ? Object.entries(labels).flatMap(([key, label]) => {
        const item = usage[key];
        return item && Number.isSafeInteger(item.requests) && item.requests > 0
          ? [`${label}已知输入/输出 ${item.input_tokens}/${item.output_tokens} token（${item.unknown_usage} 次用量未知${Number.isFinite(item.estimated_cost) ? `，估算费用 ${item.estimated_cost}` : "，费用未配置或不完整"}）`] : [];
      }) : [];
      const members = Object.entries(group?.performance?.usage?.members || {})
        .sort((a, b) => b[1].requests - a[1].requests).slice(0, 8)
        .map(([member, item]) => `成员 ${member}：${item.requests} 次，已知输入/输出 ${item.input_tokens}/${item.output_tokens} token，${item.unknown_usage} 次用量未知`);
      text("proactive-runtime-detail", detail + " 近一小时调用（重启后重计，含准备耗时）：" + [...parts, ...counters, ...members].join("；") + "。");
    }
  }
  for (const key of ["judgments_hour", "judgments_day", "replies_hour", "replies_day"]) {
    text(`proactive-${key.replaceAll("_", "-")}`, proactiveUsage(group?.[key], group?.[`${key}_limit`]));
  }
  const event = group?.last_event;
  const eventName = event && Object.hasOwn(proactiveEventNames, event.code) ? proactiveEventNames[event.code] : "暂无可识别的处理记录";
  text("proactive-last-event", event ? `${eventName} · ${proactiveTime(event.at)}` : "暂无记录");
  text("proactive-last-sent", proactiveTime(group?.last_sent_at));
  text("proactive-global-budget", `所有群共用的最近 24 小时判断额度：${proactiveUsage(status?.judgments_day, status?.judgments_day_limit)}。判断额度包含保持安静和调用失败的情况，不等于群消息数或回复数；达到上限后随时间恢复。`);
  text("proactive-runtime-updated", `状态采样：${proactiveTime(status?.generated_at)}${proactiveConfigReady ? "" : " · 本次读取失败，以上数据可能已过期"}`);
}
const proactivePersonaLimit = 6000;
function renderProactivePersonaCount() {
  // Count Unicode code points like Python's len(), including emoji as one.
  const length = Array.from($("proactive-persona").value).length;
  const over = length > proactivePersonaLimit;
  text("proactive-persona-count", `${length} / ${proactivePersonaLimit} 字${over ? " · 超出限制，请缩短后保存；内容已保留" : ""}`);
  $("proactive-persona").setAttribute("aria-invalid", String(over));
}
function renderProactiveEditor() {
  const selected = proactiveSelected;
  $("proactive-empty").hidden = Boolean(selected);
  $("proactive-group-settings").hidden = !selected;
  renderProactiveRuntime();
  if (!selected || !proactiveDraft) return;
  const member = proactiveGroups.find((group) => group.group_id === selected);
  const group = proactiveDraft.groups[selected] || proactiveDefaultGroup();
  text("proactive-group-name", member?.name || "已保存的群");
  text("proactive-group-id", `群号 ${selected}${member ? "" : " · 当前群列表中未找到，可设为仅 @ 回复"}`);
  $("proactive-mode").value = group.mode;
  document.querySelectorAll("#proactive-mode option").forEach((option) => { option.disabled = !member && option.value !== "off"; });
  $("proactive-display-name").value = group.display_name;
  $("proactive-persona").value = group.persona;
  renderProactivePersonaCount();
  $("proactive-quiet-enabled").checked = group.quiet_hours.enabled;
  $("proactive-quiet-start").value = group.quiet_hours.start;
  $("proactive-quiet-end").value = group.quiet_hours.end;
  $("proactive-quiet-fields").hidden = !group.quiet_hours.enabled;
  text("proactive-mode-note", proactiveModeNotes[group.mode]);
}
function renderProactiveGroups() {
  const list = $("proactive-group-list");
  list.replaceChildren();
  if (!proactiveDraft) return;
  const query = $("proactive-search").value.trim().toLocaleLowerCase();
  const groups = proactiveGroups.map((group) => ({...group, current: true}));
  const known = new Set(groups.map((group) => group.group_id));
  for (const group_id of Object.keys(proactiveDraft.groups)) {
    if (!known.has(group_id)) groups.push({group_id, name: "已保存的群", current: false});
  }
  const visible = groups.filter((group) => (group.name + " " + group.group_id).toLocaleLowerCase().includes(query));
  for (const group of visible) {
    const mode = proactiveDraft.groups[group.group_id]?.mode || "off";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "proactive-group";
    button.classList.toggle("is-selected", group.group_id === proactiveSelected);
    button.setAttribute("aria-pressed", String(group.group_id === proactiveSelected));
    const title = document.createElement("span");
    title.className = "proactive-group-title";
    const name = document.createElement("strong");
    name.textContent = group.name || group.group_id;
    const id = document.createElement("small");
    id.textContent = group.group_id + (group.current ? "" : " · 未在当前列表");
    title.append(name, id);
    const badge = document.createElement("span");
    badge.className = "proactive-group-mode";
    badge.dataset.enabled = String(mode !== "off");
    badge.textContent = proactiveModes[mode];
    button.append(title, badge);
    button.addEventListener("click", () => {
      if (button.disabled) return;
      proactiveSelected = group.group_id;
      renderProactiveGroups();
      renderProactiveEditor();
      controls();
    });
    list.append(button);
  }
  if (proactiveGroupsReady) text("proactive-groups-message", query && !visible.length ? "没有匹配的群。" : groups.length ? `已读取 ${proactiveGroups.length} 个当前 QQ 群，选择一个进行设置。` : "当前 QQ 尚未加入群；加入后点击刷新。");
  controls();
}
function applyProactive(value, replace = false) {
  if (!validProactiveConfig(value.config)) throw new Error("自由对话设置格式无法识别，请更新管理服务后重试。");
  proactiveConfigReady = true;
  proactiveStatus = value.status || {};
  proactiveSavedConfig = JSON.parse(JSON.stringify(value.config));
  proactiveBuiltinStyle = typeof value.builtin_style === "string" ? value.builtin_style : null;
  if (replace || !proactiveDraft || !proactiveDirty) {
    proactiveDraft = JSON.parse(JSON.stringify(value.config));
    proactiveDirty = false;
    proactiveConflict = false;
    $("proactive-enabled").checked = proactiveDraft.enabled;
    $("proactive-default-style").value = proactiveDraft.default_style || proactiveBuiltinStyle || "";
    text("proactive-default-style-note", proactiveBuiltinStyle === null ? "当前服务尚未提供默认语气，请更新后重试。" : proactiveDraft.default_style ? "当前使用已保存的自定义默认语气。" : "当前使用内置默认语气。");
    renderProactiveGroups();
    renderProactiveEditor();
  } else if (value.config.revision !== proactiveDraft.revision) {
    proactiveConflict = true;
    proactiveFeedback("设置已在其他页面修改。当前编辑已保留，请重新载入已保存设置后再调整。");
  }
  proactiveSummary();
  if (proactiveStatus.available === false) proactiveFeedback(proactiveStatus.message || "自由对话服务暂不可用，请稍后重试。");
}
async function pollProactive(epoch = authEpoch, force = false, replace = false) {
  if (!authenticated || proactiveLoading || proactiveAction || (!force && Date.now() - proactiveLastPoll < 15000)) return;
  proactiveLoading = true;
  proactiveLastPoll = Date.now();
  controls();
  try {
    const results = await Promise.allSettled([
      api("/api/proactive"),
      setupState?.qq_logged_in ? api("/api/proactive/groups") : Promise.reject(new Error("请先登录 QQ，再选择开放自由对话的群。"))
    ]);
    if (epoch !== authEpoch || !authenticated) return;
    const [configResult, groupsResult] = results;
    if (groupsResult.status === "fulfilled" && Array.isArray(groupsResult.value.groups) && groupsResult.value.groups.every((group) => /^\d+$/.test(String(group.group_id)) && typeof group.name === "string")) {
      proactiveGroups = groupsResult.value.groups.map((group) => ({group_id: String(group.group_id), name: group.name}));
      proactiveGroupsReady = true;
    } else {
      proactiveGroupsReady = false;
      text("proactive-groups-message", groupsResult.status === "rejected" ? groupsResult.reason.message : "群列表读取失败，请刷新重试。已保存的设置不会被覆盖。");
    }
    if (configResult.status === "fulfilled") {
      try { applyProactive(configResult.value, replace); }
      catch (error) { proactiveConfigReady = false; proactiveFeedback(error.message); }
    } else {
      proactiveConfigReady = false;
      proactiveFeedback(configResult.reason.message);
    }
    if (proactiveDraft) renderProactiveGroups();
  } finally {
    proactiveLoading = false;
    renderProactiveRuntime();
    controls();
  }
}
function editProactiveGroup() {
  if (!proactiveDraft || !proactiveSelected || $("proactive-group-settings").disabled) return;
  if (!Object.hasOwn(proactiveDraft.groups, proactiveSelected) && Object.keys(proactiveDraft.groups).length >= 100) {
    proactiveFeedback("最多保存 100 个群的独立设置。请先整理已保存的群设置。");
    renderProactiveEditor();
    return;
  }
  const group = {
    mode: $("proactive-mode").value,
    display_name: $("proactive-display-name").value,
    persona: $("proactive-persona").value,
    quiet_hours: {enabled: $("proactive-quiet-enabled").checked, start: $("proactive-quiet-start").value, end: $("proactive-quiet-end").value, timezone: "Asia/Shanghai"}
  };
  proactiveDraft.groups[proactiveSelected] = group;
  renderProactivePersonaCount();
  proactiveDirty = true;
  $("proactive-quiet-fields").hidden = !group.quiet_hours.enabled;
  text("proactive-mode-note", proactiveModeNotes[group.mode]);
  proactiveSummary();
  renderProactiveGroups();
}
function summary(name, label, detail, state = "neutral") {
  text("summary-" + name, label);
  text("summary-" + name + "-detail", detail);
  $("summary-" + name).closest(".summary-card").dataset.tone = state;
}
function renderSetup(state) {
  const qq = qqHealth(state);
  const runtimeReady = state.plugin_runtime_healthy === true;
  const recovering = state.configured && (!state.plugins_ready || !runtimeReady);
  const ready = state.stage === "ready" && state.configured && state.plugins_ready && qq.connected && runtimeReady;
  const failed = state.stage === "error";
  const label = failed ? "需要处理" : ready ? "运行就绪" : state.stage === "configuring" ? "配置中" : recovering ? "组件恢复中" : !state.plugins_ready ? "初始化中" : !state.configured ? "等待配置" : qq.label;
  text("global-state", label);
  text("sidebar-status", label);
  const checkingQQ = !failed && state.configured && state.plugins_ready && state.stage !== "configuring";
  text("status", recovering ? "功能组件暂未确认可用，正在检测并自动恢复。" : checkingQQ && !ready ? qq.detail : state.message || "管理服务已连接。");
  tone("status", failed ? "error" : ready ? "success" : "neutral");
  connectionTone(failed ? "error" : ready ? "success" : "warning");
  text("last-sync", qq.checkedAt ? "QQ 检查于 " + new Date(qq.checkedAt * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}) + (qq.fresh ? "" : " · 状态已过期") : "QQ 状态尚未确认");
  summary("qq", qq.label, qq.detail, qq.tone);
  summary("ai", state.configured ? "已配置" : "待配置", state.settings?.chat_model || "填写接口地址与模型", state.configured ? "success" : "neutral");
  summary("plugins", state.plugins_ready && runtimeReady ? "已就绪" : state.plugins_ready ? "恢复中" : "初始化中", state.plugins_ready && runtimeReady ? "功能组件心跳正常" : state.plugins_ready ? "组件已安装，正在确认通信是否恢复" : "首次安装可能需要几分钟", state.plugins_ready && runtimeReady ? "success" : "warning");
  $("ready").hidden = !ready;
  const showQR = Boolean(state.qr_image) && qq.fresh && ["waiting_login", "offline"].includes(qq.code);
  $("qr").hidden = !showQR;
  $("connection-art").hidden = showQR;
  $("connection-card").dataset.state = qq.connected ? "connected" : qq.loggedIn ? "pending" : "waiting";
  if (showQR && $("qr").src !== state.qr_image) $("qr").src = state.qr_image;
  if (!showQR) $("qr").removeAttribute("src");
  text("qq-status", qq.connected ? runtimeReady ? "QQ 已连接，随时可以开始聊" : "QQ 已连接，功能组件正在恢复" : showQR ? qq.code === "offline" ? "QQ 已离线，请重新扫码并在手机上确认" : "使用手机 QQ 扫码，并在手机上确认" : qq.code === "waiting_login" ? "正在等待登录二维码…" : qq.detail);
  if (!loaded && state.settings?.base_url) {
    for (const element of $("settings").querySelectorAll('input[name="features"]')) {
      element.checked = (state.settings.features || ["memory", "files", "vision", "image", "search", "comic"]).includes(element.value);
    }
    for (const [key, value] of Object.entries(state.settings)) {
      if (key !== "api_key" && key !== "features" && $("settings").elements[key]) {
        const inheritsChat = ["vision_model", "search_model", "decision_model", "reconcile_model"].includes(key) && value === state.settings.chat_model;
        $("settings").elements[key].value = inheritsChat ? "" : value;
      }
    }
    $("settings").elements.api_key.placeholder = "已保存 · 留空保留原密钥";
    loaded = true;
  }
  if ($("feature-status") && state.capabilities) {
    const names = {memory:"记忆", files:"文件", vision:"视觉", image:"图片", search:"联网", comic:"漫画"};
    text("feature-status", Object.entries(names).map(([key, label]) => `${label}：${state.capabilities[key] === "ready" ? "配置就绪" : state.capabilities[key] === "error" ? "需要检查" : "未启用"}`).join(" · "));
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
  const count = Number.isInteger(value.keep_backups) ? `保留最近 ${value.keep_backups} 次更新的部署记录` : "保留必要的部署记录";
  const hasCounts = [value.removed_jobs, value.removed_images].every(Number.isInteger);
  const removed = hasCounts ? `；本次清理 ${value.removed_jobs} 份历史记录、${value.removed_images} 个镜像` : "";
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
  const steps = {queued: 0, building: 0, updating: 0, backing_up: 1, stopping: 1, restarting: 1, verifying: 2};
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
    qqStatusReceivedAt = Date.now();
    authenticated = true;
    $("unlock").hidden = true;
    $("console-content").hidden = false;
    renderSetup(state);
  } catch (error) {
    if (epoch !== authEpoch) return;
    const checkedAt = qqHealth(setupState).checkedAt;
    setupState = null;
    text("status", updateExpected ? "正在等待更新后的管理服务恢复…" : error.message);
    tone("status", "warning");
    text("global-state", "连接中断");
    text("sidebar-status", "等待重连");
    connectionTone("warning");
    summary("qq", "状态待确认", "管理服务恢复后会重新检查 QQ 在线状态", "warning");
    text("last-sync", checkedAt ? "QQ 检查于 " + new Date(checkedAt * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}) + " · 本次读取失败" : "QQ 状态尚未确认");
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
  pollPromise = Promise.allSettled([pollSetup(epoch), pollUpdate(epoch)]).then(() => pollProactive(epoch)).finally(() => {
    polling = false;
    pollPromise = null;
    $("refresh-state").classList.remove("is-loading");
    $("refresh-state").disabled = false;
  });
  return pollPromise;
}
$("proactive-search").addEventListener("input", renderProactiveGroups);
$("proactive-refresh").addEventListener("click", async () => {
  if ($("proactive-refresh").disabled) return;
  await pollProactive(authEpoch, true);
});
$("proactive-enabled").addEventListener("change", () => {
  if ($("proactive-enabled").disabled || !proactiveDraft) return;
  proactiveDraft.enabled = $("proactive-enabled").checked;
  proactiveDirty = true;
  proactiveSummary();
  controls();
});
$("proactive-default-style").addEventListener("input", () => {
  if ($("proactive-default-style").disabled || !proactiveDraft) return;
  const value = $("proactive-default-style").value.trim();
  proactiveDraft.default_style = value === proactiveBuiltinStyle ? "" : value;
  proactiveDirty = true;
  text("proactive-default-style-note", value ? "默认语气已修改，保存后生效。" : "留空将在保存后恢复内置默认语气。");
  proactiveSummary();
  controls();
});
$("proactive-reset-style").addEventListener("click", () => {
  if ($("proactive-reset-style").disabled || !proactiveDraft || proactiveBuiltinStyle === null) return;
  proactiveDraft.default_style = "";
  $("proactive-default-style").value = proactiveBuiltinStyle;
  proactiveDirty = true;
  text("proactive-default-style-note", "已恢复内置默认语气，保存后生效；群内独立性格保留。");
  proactiveSummary();
  controls();
});
$("proactive-reset-group").addEventListener("click", () => {
  if ($("proactive-reset-group").disabled || !proactiveDraft || !proactiveSelected) return;
  delete proactiveDraft.groups[proactiveSelected];
  proactiveDirty = true;
  renderProactiveEditor();
  renderProactiveGroups();
  proactiveSummary();
  proactiveFeedback("已恢复此群默认设置，保存后将仅响应 @ 和 /ai。", "success");
});
for (const id of ["proactive-mode", "proactive-quiet-enabled", "proactive-quiet-start", "proactive-quiet-end"]) {
  $(id).addEventListener("change", editProactiveGroup);
}
for (const id of ["proactive-display-name", "proactive-persona"]) {
  $(id).addEventListener("input", editProactiveGroup);
}
$("proactive-reload").addEventListener("click", async () => {
  if ($("proactive-reload").disabled) return;
  if (proactiveDirty && !window.confirm("重新载入将丢弃当前未保存的自由对话修改，改用服务器上已保存的设置。继续吗？")) return;
  await pollProactive(authEpoch, true, true);
  if (!proactiveDirty && !proactiveConflict && proactiveConfigReady) proactiveFeedback("已重新载入服务器上保存的设置。", "success");
});
$("proactive-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("proactive-save").disabled || !proactiveDraft) return;
  const epoch = authEpoch;
  const config = JSON.parse(JSON.stringify(proactiveDraft));
  const oversizedPersona = Object.entries(config.groups).find(([, group]) => Array.from(group.persona).length > proactivePersonaLimit);
  if (oversizedPersona) { proactiveFeedback(`群 ${oversizedPersona[0]} 的性格与交流习惯超过 ${proactivePersonaLimit} 字，请缩短后保存。输入内容已保留。`); return; }
  const invalidQuiet = Object.values(config.groups).some((group) => group.quiet_hours.enabled && (!group.quiet_hours.start || !group.quiet_hours.end || group.quiet_hours.start === group.quiet_hours.end));
  if (invalidQuiet) { proactiveFeedback("请填写完整的安静时段，开始和结束时间不能相同。"); return; }
  proactiveAction = true;
  proactiveFeedback("");
  controls();
  try {
    const value = await api("/api/proactive", config, 60000);
    if (epoch !== authEpoch || !authenticated) return;
    applyProactive(value, true);
    proactiveFeedback("自由对话设置已保存。只有明确选择的群会开放主动参与。", "success");
  } catch (error) {
    if (epoch !== authEpoch) return;
    if (error.status === 409) proactiveConflict = true;
    proactiveFeedback(error.status === 409 ? "设置已发生变化，当前编辑已保留。请重新载入已保存设置后再调整。" : error.message);
  } finally { proactiveAction = false; controls(); }
});
$("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("save").disabled) return;
  const form = event.currentTarget;
  text("form-message", "");
  configAction = true; controls();
  try {
    const data = new FormData(form);
    const config = Object.fromEntries(data);
    config.features = data.getAll("features");
    await api("/api/configure", config);
    for (const element of form.querySelectorAll('input[type="password"]')) element.value = "";
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
  if (!window.confirm(`更新到 ${revision.slice(0, 12)}？\n\n切换版本会短暂停机并中断正在进行的任务，请在空闲时更新。更新不会备份业务数据；验证失败时尝试恢复旧版服务，业务数据不会回退。请等待页面恢复并确认结果。`)) return;
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
  proactiveDraft = null;
  proactiveSavedConfig = null;
  proactiveStatus = null;
  proactiveGroups = [];
  proactiveSelected = "";
  proactiveDirty = false;
  proactiveConflict = false;
  proactiveConfigReady = false;
  proactiveGroupsReady = false;
  proactiveLastPoll = 0;
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
  ["overview", "models", "connection", "proactive", "requests", "knowledge", "maintenance", "advanced"].forEach((id) => observer.observe($(id)));
}
const requestPhases = {received:"收到请求",queued:"排队",preparing:"准备上下文",runtime_wait:"等待功能组件",running:"处理消息",model:"生成回复",tool_wait:"等待工具",tool:"执行工具",retry_wait:"准备有限重试",resuming:"等待继续回复",sending:"发送回复",delivered:"发送已确认",ended:"处理结束"};
const requestStatuses = {active:"处理中",completed:"回复已发送",processed:"流程已结束",failed:"处理失败",cancelled:"已取消",interrupted:"执行中断",unconfirmed:"结果未确认",rejected:"未能排队"};
const requestReasons = {queue_full:"请求队列已满",queue_timeout:"排队超时",execution_timeout:"请求总时限已到",tool_timeout:"工具执行超时",tool_queue_timeout:"等待工具超时",runtime_unavailable:"功能组件暂不可用",upstream_unavailable:"上游服务暂不可用",upstream_timeout:"服务响应超时",rate_limited:"服务调用限流",authentication:"接口认证失败",permission:"权限或工作区绑定已失效",network:"连接异常",tool_failed:"工具执行失败",pipeline_failed:"消息处理异常",no_confirmed_reply:"流程结束，未记录文本回复的发送确认",delivery_unconfirmed:"回复发送未确认",restart_before_execution:"服务重启，尚未开始执行",restart_interrupted:"服务重启，执行被中断",restart_unconfirmed:"服务重启，操作结果未确认",cancelled:"用户取消",shutdown:"服务关闭，执行已中断",capacity:"长任务容量已满",admission_failed:"请求未能开始执行"};
let requestsSequence = 0, requestDetailSequence = 0, requestsCursor = null, requestsBusy = false;
function clearRequests() {
  requestsSequence += 1;
  requestDetailSequence += 1;
  requestsCursor = null;
  requestsBusy = false;
  $("requests-list").replaceChildren();
  $("request-detail").replaceChildren();
  $("request-detail").hidden = true;
  $("requests-older").hidden = true;
  $("requests-refresh").disabled = false;
}
function requestTime(value) {
  const time = new Date(Number(value) * 1000);
  return Number.isFinite(time.getTime()) ? time.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",hour12:false}) : "时间未知";
}
function requestNode(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}
async function showRequest(requestId) {
  const sequence = ++requestDetailSequence, epoch = authEpoch;
  const box = $("request-detail");
  box.hidden = false;
  box.replaceChildren(requestNode("p", "正在读取处理过程…"));
  try {
    const data = await api("/api/requests?" + new URLSearchParams({request_id:requestId}));
    if (!authenticated || epoch !== authEpoch || sequence !== requestDetailSequence) return;
    const item = data.requests?.[0];
    if (!data.available || !item) throw Error("该记录已过期或暂不可用，请刷新列表。");
    const events = document.createElement("ol");
    for (const event of item.events || []) {
      const parts = [requestTime(event.at), requestPhases[event.phase] || "未知阶段"];
      if (event.tool) parts.push(event.tool);
      if (event.attempt) parts.push("第 " + event.attempt + " 次尝试");
      if (event.reason) parts.push(requestReasons[event.reason] || "未分类异常");
      events.append(requestNode("li", parts.join(" · ")));
    }
    box.replaceChildren(requestNode("h3", "请求 " + item.request_id), events);
    if (item.timing?.phase_seconds) {
      const parts = Object.entries(item.timing.phase_seconds).filter(([, seconds]) => Number.isFinite(seconds) && seconds > 0)
        .map(([phase, seconds]) => `${requestPhases[phase] || "其他阶段"} ${seconds} 秒`);
      box.append(requestNode("p", "阶段耗时：" + parts.join("；") + (item.timing.complete ? "。" : "。早期事件已裁剪，统计不完整。")));
    }
    if (item.operation_unconfirmed) box.append(requestNode("p", "有操作未确认结果。请先核对实际产物，避免重复执行。"));
  } catch (error) {
    if (authenticated && epoch === authEpoch && sequence === requestDetailSequence) box.replaceChildren(requestNode("p", error.message));
  }
}
async function loadRequests(older = false) {
  if (!authenticated) return;
  const cursor = older ? requestsCursor : null;
  clearRequests();
  const sequence = requestsSequence, epoch = authEpoch;
  requestsBusy = true;
  $("requests-refresh").disabled = true;
  text("requests-note", "正在读取请求记录…");
  const filters = {limit:"50"};
  if ($("requests-group").value.trim()) filters.group = $("requests-group").value.trim();
  if ($("requests-status").value) filters.status = $("requests-status").value;
  if (cursor) filters.before = cursor;
  try {
    const data = await api("/api/requests?" + new URLSearchParams(filters));
    if (!authenticated || epoch !== authEpoch || sequence !== requestsSequence) return;
    if (!data.available) throw Error("暂未启用请求记录，或机器人尚未完成配置。");
    for (const item of data.requests || []) {
      const row = document.createElement("article");
      row.className = "request-row";
      const button = requestNode("button", (requestStatuses[item.status] || "未知状态") + " · 群 " + item.group_id + " · " + requestTime(item.created));
      button.type = "button";
      button.className = "secondary small";
      button.addEventListener("click", () => showRequest(item.request_id));
      const detail = [item.origin === "continuation" ? "续问查询" : "@ /ai 请求", "发起人 " + item.sender, requestPhases[item.phase] || "未知阶段", "耗时 " + item.elapsed_seconds + " 秒"];
      if (item.reason) detail.push(requestReasons[item.reason] || "未分类异常");
      row.append(button, requestNode("p", detail.join(" · ")));
      $("requests-list").append(row);
    }
    requestsCursor = data.next_before || null;
    $("requests-older").hidden = !requestsCursor;
    const warning = data.write_degraded ? "记录存储曾出现异常，以下内容可能不完整。 " : "";
    text("requests-note", warning + (data.requests?.length ? "点击记录查看过程。" : "当前筛选条件下暂无记录。") + "采样于 " + requestTime(data.sampled_at) + "（北京时间）" + (cursor ? " · 较早记录，刷新可返回最新。" : ""));
  } catch (error) {
    if (authenticated && epoch === authEpoch && sequence === requestsSequence) text("requests-note", error.message);
  } finally {
    if (epoch === authEpoch && sequence === requestsSequence) {
      requestsBusy = false;
      $("requests-refresh").disabled = false;
    }
  }
}
$("requests-open").addEventListener("toggle", () => { if ($("requests-open").open) loadRequests(); });
$("requests-refresh").addEventListener("click", () => loadRequests());
$("requests-older").addEventListener("click", () => loadRequests(true));
$("requests-group").addEventListener("change", () => loadRequests());
$("requests-status").addEventListener("change", () => loadRequests());
poll();
setInterval(poll, 4000);
setInterval(expireQQStatus, 1000);
