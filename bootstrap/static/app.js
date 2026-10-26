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
let settingsDirty = false;
let settingsConflict = false;
let settingsRevision = null;
let settingsPending = false;
let settingsReloading = false;
let settingsDraftRestored = false;
let groupDraftRestored = false;
let activeGroupTab = "persona";
let qrAction = false;
let adminAction = false;
let proactiveDraft = null;
let proactiveGroups = [];
let proactiveSelected = "";
let memorySequence = 0, memorySelected = null, memoryDirty = false, memoryBusy = false;
let memoryOffset = 0, memoryNext = null, memoryGroup = "";
let taskTab = "requests", tasksSequence = 0, tasksBusy = false, tasksCursor = null, tasksOlder = false;
let proactiveDirty = false;
let proactiveConflict = false;
let proactiveConfigReady = false;
let proactiveGroupsReady = false;
let proactiveAction = false;
let memoryResetAction = false;
let memoryResetFeedback = null;
let proactiveLoading = false;
let proactiveReplacing = false;
let proactiveStatus = null;
let proactiveSavedConfig = null;
let proactiveBuiltinStyle = null;
let proactiveLastPoll = 0;
let proactiveGroupsLastPoll = 0;
const proactiveModes = {off: "仅 @ 回复", quiet: "安静参与", natural: "自然参与", active: "活跃参与"};
const proactiveModeNotes = {
  off: "保留原来的 @ 和 /ai 回复方式，不判断是否主动插话。",
  quiet: "回应直接称呼、引用或对自己的追问，不主动加入其他人的话题。",
  natural: "自然接续对话，也会选择性加入讨论；普通消息和表情不会打断已开始的回复。",
  active: "更积极地接话和参与讨论，发言间隔更短、额度更高；仍会合并消息，避免逐条刷屏。"
};
const proactiveRuntimeStates = {
  memory_reset_pending: ["记忆清除未完成", "error", "本群已暂停接话，旧记忆不会继续使用。请点击“清空本群全部记忆”重试，完成后恢复。"],
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
  clearMemoryView();
  clearTasks();
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
  $("proactive-default-examples").replaceChildren();
  $("proactive-examples").replaceChildren();
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
function readDraft(kind) {
  try { return JSON.parse(sessionStorage.getItem("qunling-console-draft:" + kind) || "null"); }
  catch { return null; }
}
function writeDraft(kind, value) {
  try {
    const key = "qunling-console-draft:" + kind;
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch { /* Browser storage may be full/disabled; leave protection still works. */ }
}
function persistGroupDraft() {
  writeDraft("groups", proactiveDirty && proactiveDraft ? {config: proactiveDraft, selected: proactiveSelected} : null);
}
const providerRoles = ["vision", "image", "search", "decision", "reconcile"];
const inheritedModels = ["vision_model", "search_model", "decision_model", "reconcile_model"];
const featureNames = ["memory", "files", "vision", "image", "search", "comic"];
const aiSecretFields = ["api_key", ...providerRoles.map(role => role + "_api_key")];
const aiDraftFields = ["base_url", "chat_model", "image_model", ...inheritedModels, ...providerRoles.map(role => role + "_base_url"), "image_size", "image_quality", "image_timeout_seconds", "search_timeout_seconds", "group_context_hours", "group_context_max_messages"];
const featureInputs = () => [...$("settings").querySelectorAll('input[name="features"]')];
function persistSettingsDraft() {
  const values = {};
  for (const key of aiDraftFields) if ($("settings").elements[key]) values[key] = $("settings").elements[key].value;
  values.features = featureInputs().filter(field => field.checked).map(field => field.value);
  writeDraft("ai", settingsDirty ? {values, revision: settingsRevision} : null);
  text("settings-dirty", settingsConflict ? "服务器配置已变化，草稿保留；重新载入后再保存" : settingsDirty ? "有未保存的修改 · 密钥不写入浏览器草稿" : "已保存的密钥可留空保留");
}
function hydrateSettings(state) {
  for (const [key, value] of Object.entries(state.settings || {})) {
    if (aiDraftFields.includes(key) && $("settings").elements[key]) {
      const inheritsChat = inheritedModels.includes(key) && value === state.settings.chat_model;
      $("settings").elements[key].value = inheritsChat ? "" : value;
    }
  }
  const features = state.settings?.features || (state.configured && state.settings?.image_model ? featureNames : []);
  for (const field of featureInputs()) field.checked = features.includes(field.value);
  for (const key of aiSecretFields) {
    const field = $("settings").elements[key];
    if (field) {
      field.value = "";
      field.type = "password";
      field.placeholder = state.settings?.[key + "_saved"] ? "已保存 · 留空保留原密钥" : "填写服务提供方的 API Key";
    }
  }
  $("show-key").setAttribute("aria-pressed", "false");
  $("show-key").setAttribute("aria-label", "显示 API Key");
  settingsRevision = state.settings_revision || null;
  settingsDirty = settingsConflict = false;
  loaded = true;
  renderProviderFields();
}
function renderSettingsState(state) {
  if (settingsPending && !configAction && state.stage !== "configuring") {
    settingsPending = false;
    if (state.stage === "error") {
      text("form-message", state.message || "配置未成功应用，输入已保留，请检查后重试。");
      tone("form-message", "error");
    } else if (state.configured) {
      hydrateSettings(state);
      text("form-message", "AI 与工具设置已应用。保存配置不会调用模型验证服务能力。");
      tone("form-message", "success");
    }
  }
  if (!loaded && !settingsDirty) hydrateSettings(state);
  if (!settingsDraftRestored) {
    settingsDraftRestored = true;
    const cached = readDraft("ai");
    if (cached?.values && typeof cached.values === "object" && !Array.isArray(cached.values)) {
      for (const key of aiDraftFields) if (typeof cached.values[key] === "string" && $("settings").elements[key]) $("settings").elements[key].value = cached.values[key];
      if (Array.isArray(cached.values.features)) {
        for (const field of featureInputs()) field.checked = cached.values.features.includes(field.value);
      }
      if (cached.values.search_provider === "shared") $("settings").elements.search_base_url.value = "";
      settingsRevision = cached.revision || null;
      settingsDirty = true;
      text("form-message", "已恢复未保存草稿；新输入的密钥不会暂存，需重新填写。尚未应用到机器人。");
      tone("form-message", "neutral");
    }
  }
  if (!settingsPending && !configAction && (state.settings_revision || null) !== settingsRevision) {
    if (settingsDirty) settingsConflict = true;
    else hydrateSettings(state);
  }
  persistSettingsDraft();
}
function renderProviderFields() {
  for (const role of providerRoles) {
    const endpoint = $("settings").elements[role + "_base_url"];
    const secret = $("settings").elements[role + "_api_key"];
    if (!endpoint || !secret) continue;
    const sameEndpoint = endpoint.value.trim().replace(/\/+$/, "") === (setupState?.settings?.[role + "_base_url"] || "");
    secret.required = Boolean(endpoint.value.trim()) && !(sameEndpoint && setupState?.settings?.[role + "_api_key_saved"]);
  }
  $("settings").elements.image_model.required = featureInputs().some(field => field.value === "image" && field.checked);
}
function renderPersonaPreview() {
  const saved = proactiveSavedConfig;
  const group = saved?.groups?.[proactiveSelected];
  const persona = group?.persona?.trim();
  const independent = Boolean(persona || group?.examples?.length);
  const style = independent ? persona || "" : saved?.default_style || proactiveBuiltinStyle || "";
  const examples = (independent ? group?.examples : saved?.default_examples) || [];
  const samples = examples.map((turns, index) => `示例 ${index + 1}\n` + turns.map((turn, i) => `${i % 2 ? "机器人" : "群友"}：${turn}`).join("\n")).join("\n\n");
  text("persona-source", independent ? "本群独立性格" : "继承默认语气");
  text("persona-effective", [style, samples].filter(Boolean).join("\n\n") || "尚未读取到生效设置。");
  text("persona-effective-note", proactiveDirty ? "这里展示服务器已保存的内容；正在编辑的草稿尚未生效。" : "参与判断、@ 回复、自由对话及工具回复均使用以下内容。");
}
function setGroupTab(tab) {
  if (!["persona", "activity", "memory", "defaults"].includes(tab)) return;
  activeGroupTab = tab;
  $("group-reset-action").hidden = tab === "memory" || tab === "defaults";
  $("group-save-bar").hidden = tab === "memory";
  document.querySelectorAll("[data-group-tab]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.groupTab === tab)));
  document.querySelectorAll("[data-group-panel]").forEach(panel => { panel.hidden = panel.dataset.groupPanel !== tab; });
  renderProactiveEditor();
  if (tab === "memory" && proactiveSelected && memoryGroup !== proactiveSelected) loadMemory();
}
function controls() {
  const updating = updateAction || Boolean(updateExpected) || Boolean(updateState?.busy);
  const aiBusy = configAction || settingsPending || settingsReloading || setupState?.stage === "configuring";
  $("save").disabled = !authenticated || aiBusy || settingsConflict || proactiveAction || updating || !setupState || setupState.stage === "starting";
  text("save-label", aiBusy ? "正在应用配置…" : "保存 AI 与工具设置");
  for (const field of document.querySelectorAll("#settings input, #settings select")) field.disabled = !authenticated || aiBusy;
  renderProviderFields();
  $("settings-reload").hidden = !settingsDirty && !settingsConflict;
  $("settings-reload").disabled = !authenticated || aiBusy || !setupState?.settings;
  $("check-update").disabled = !authenticated || updating || !updateState?.available;
  $("apply-update").disabled = !authenticated || configAction || proactiveAction || updating || !updateState?.available || !updateState.update_available || !updateState.latest_revision || !setupState || ["starting", "configuring"].includes(setupState.stage);
  $("refresh").disabled = !authenticated || qrAction || updating || !setupState || qqHealth(setupState).loggedIn;
  $("admin").disabled = !authenticated || adminAction;
  // Reading status is not a mutation. Offline/conflicted drafts stay editable;
  // only applying them needs fresh membership, service state and a revision.
  const editingBlocked = !authenticated || proactiveAction || proactiveReplacing || !proactiveDraft;
  const proactiveBlocked = editingBlocked || !setupState?.qq_logged_in || aiBusy || updating || proactiveLoading || !proactiveConfigReady || !proactiveGroupsReady || proactiveConflict || proactiveStatus?.available === false;
  document.querySelectorAll("[data-proactive-edit], .proactive-group").forEach((element) => { element.disabled = editingBlocked; });
  $("proactive-group-settings").disabled = editingBlocked || !proactiveSelected;
  $("proactive-default-style").disabled = editingBlocked || proactiveBuiltinStyle === null;
  $("proactive-reset-style").disabled = editingBlocked || proactiveBuiltinStyle === null;
  $("proactive-reset-group").disabled = editingBlocked || !proactiveSelected || !proactiveDraft || !Object.hasOwn(proactiveDraft.groups, proactiveSelected);
  $("proactive-clear-memory").disabled = proactiveBlocked || !proactiveSelected || !proactiveGroups.some(group => group.group_id === proactiveSelected);
  text("proactive-clear-memory", memoryResetAction ? "正在清空…" : "清空本群全部记忆");
  $("proactive-save").disabled = proactiveBlocked || !proactiveDirty;
  $("proactive-refresh").disabled = !authenticated || !setupState?.qq_logged_in || updating || proactiveLoading || proactiveAction;
  $("proactive-search").disabled = !proactiveDraft;
  $("proactive-reload").disabled = !authenticated || updating || proactiveAction || proactiveLoading;
  $("proactive-reload").hidden = !proactiveDirty && !proactiveConflict;
  text("proactive-save-label", proactiveAction && !memoryResetAction ? "正在保存…" : "保存群聊设置");
}
function proactiveDefaultGroup() {
  return {mode: "off", display_name: "群灵", persona: "", examples: [], quiet_hours: {enabled: false, start: "23:00", end: "08:00", timezone: "Asia/Shanghai"}};
}
function validExamples(value) {
  return value === undefined || (Array.isArray(value) && value.length <= 6 && value.every(turns =>
    Array.isArray(turns) && turns.length >= 2 && turns.length <= 12 && turns.length % 2 === 0 && turns.every(turn => typeof turn === "string")));
}
function exampleValidationError(value) {
  if (!validExamples(value)) return "示例最多 6 组，每组需要 1–6 轮完整对话。";
  const turns = (value || []).flat();
  if (turns.some(turn => !turn.trim() || Array.from(turn).length > 1500)) return "示例每条消息需要 1–1500 字，请补全或删除空示例。";
  if (turns.reduce((sum, turn) => sum + Array.from(turn.trim()).length, 0) > 6000) return "示例合计超过 6000 字，请缩短后保存。";
  return "";
}
function validProactiveConfig(value) {
  return value?.version === 1 && typeof value.revision === "string" && typeof value.enabled === "boolean" &&
    (value.default_style === undefined || typeof value.default_style === "string") && validExamples(value.default_examples) &&
    value.groups && !Array.isArray(value.groups) && typeof value.groups === "object" &&
    Object.entries(value.groups).every(([id, group]) => /^\d+$/.test(id) && group && Object.hasOwn(proactiveModes, group.mode) &&
      typeof group.display_name === "string" && typeof group.persona === "string" && validExamples(group.examples) && typeof group.quiet_hours?.enabled === "boolean" &&
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
  persistGroupDraft();
  renderPersonaPreview();
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
  $("proactive-runtime").hidden = !authenticated || !proactiveSelected || activeGroupTab !== "activity";
  if (!authenticated || !proactiveSelected) return;
  const saved = proactiveSavedConfig;
  const status = proactiveStatus;
  const group = status?.groups?.[proactiveSelected];
  const mode = saved?.groups?.[proactiveSelected]?.mode || "off";
  let code = group?.status_code || "unknown";
  if (!proactiveConfigReady || !status) code = "unavailable";
  else if (status.storage_healthy === false) code = "storage_error";
  else if (status.available === false) code = "unavailable";
  else if (Array.isArray(status.memory_reset_pending) && status.memory_reset_pending.includes(proactiveSelected)) code = "memory_reset_pending";
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
  if (!["off", "unavailable", "disconnected", "storage_error", "unknown", "memory_reset_pending"].includes(code)
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
  const memoryFeedback = memoryResetFeedback?.group === proactiveSelected ? memoryResetFeedback : null;
  text("proactive-memory-message", memoryFeedback?.message || "");
  tone("proactive-memory-message", memoryFeedback?.tone || "neutral");
  const selected = proactiveSelected;
  $("proactive-empty").hidden = Boolean(selected) || activeGroupTab === "defaults";
  $("proactive-group-settings").hidden = !selected || activeGroupTab === "defaults";
  renderPersonaPreview();
  renderProactiveRuntime();
  renderDialogueExamples(true);
  renderDialogueExamples(false);
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
      if (group.group_id !== proactiveSelected && !leaveMemory()) return;
      proactiveSelected = group.group_id;
      renderProactiveGroups();
      renderProactiveEditor();
      persistGroupDraft();
      controls();
      if (activeGroupTab === "memory" && memoryGroup !== proactiveSelected) loadMemory();
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
  if (!groupDraftRestored) {
    groupDraftRestored = true;
    const cached = readDraft("groups");
    if (!replace && validProactiveConfig(cached?.config)) {
      proactiveDraft = cached.config;
      proactiveDirty = true;
      proactiveSelected = /^\d+$/.test(cached.selected || "") ? cached.selected : "";
      $("proactive-enabled").checked = proactiveDraft.enabled;
      $("proactive-default-style").value = proactiveDraft.default_style || proactiveBuiltinStyle || "";
      renderProactiveEditor();
      proactiveFeedback("已恢复此标签页的未保存草稿；尚未应用到机器人。", "neutral");
    }
  }
  if (replace || !proactiveDraft || (!proactiveDirty && proactiveDraft.revision !== value.config.revision)) {
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
    proactiveFeedback("设置已在其他页面修改。草稿已保留，可复制需要的内容，重新载入已保存设置后再调整。");
  }
  proactiveSummary();
  if (proactiveStatus.available === false) proactiveFeedback(proactiveStatus.message || "自由对话服务暂不可用，请稍后重试。");
}
async function pollProactive(epoch = authEpoch, force = false, replace = false) {
  if (!authenticated || proactiveLoading || proactiveAction || (!force && Date.now() - proactiveLastPoll < 15000)) return;
  proactiveLoading = true;
  proactiveReplacing = replace;
  proactiveLastPoll = Date.now();
  controls();
  try {
    const refreshGroups = force || !proactiveGroupsReady || Date.now() - proactiveGroupsLastPoll > 300000;
    const results = await Promise.allSettled([
      api("/api/proactive"),
      refreshGroups ? (setupState?.qq_logged_in ? api("/api/proactive/groups") : Promise.reject(new Error("QQ 未登录；可继续编辑已有草稿，连接恢复后再保存。"))) : Promise.resolve({groups: proactiveGroups})
    ]);
    if (epoch !== authEpoch || !authenticated) return;
    const [configResult, groupsResult] = results;
    if (groupsResult.status === "fulfilled" && Array.isArray(groupsResult.value.groups) && groupsResult.value.groups.every((group) => /^\d+$/.test(String(group.group_id)) && typeof group.name === "string")) {
      proactiveGroups = groupsResult.value.groups.map((group) => ({group_id: String(group.group_id), name: group.name}));
      proactiveGroupsReady = true;
      if (refreshGroups) proactiveGroupsLastPoll = Date.now();
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
    if (proactiveDraft && refreshGroups) renderProactiveGroups();
  } finally {
    proactiveLoading = false;
    proactiveReplacing = false;
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
    examples: proactiveDraft.groups[proactiveSelected]?.examples || [],
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
function exampleOwner(defaults, create = false) {
  if (!proactiveDraft) return null;
  if (defaults) return proactiveDraft;
  if (!proactiveSelected) return null;
  if (!proactiveDraft.groups[proactiveSelected] && create) {
    if (Object.keys(proactiveDraft.groups).length >= 100) {
      proactiveFeedback("最多保存 100 个群的独立设置。"); return null;
    }
    proactiveDraft.groups[proactiveSelected] = proactiveDefaultGroup();
  }
  return proactiveDraft.groups[proactiveSelected] || null;
}
function changeDialogueExamples(defaults, change, groupId = proactiveSelected, rerender = true) {
  if (!authenticated || !proactiveConfigReady || (defaults ? $("proactive-default-style").disabled : $("proactive-group-settings").disabled || groupId !== proactiveSelected)) return;
  const owner = exampleOwner(defaults, true);
  if (!owner) return;
  const key = defaults ? "default_examples" : "examples";
  const examples = structuredCloneExamples(owner[key] || []);
  change(examples);
  owner[key] = examples;
  proactiveDirty = true;
  if (rerender) renderDialogueExamples(defaults);
  else renderExamplesCount(defaults);
  proactiveSummary(); renderPersonaPreview(); controls();
}
function structuredCloneExamples(examples) { return examples.map(turns => [...turns]); }
function renderExamplesCount(defaults) {
  const examples = exampleOwner(defaults)?.[defaults ? "default_examples" : "examples"] || [];
  const count = examples.reduce((sum, turns) => sum + turns.reduce((n, turn) => n + Array.from(turn).length, 0), 0);
  const id = defaults ? "proactive-default-examples-note" : "proactive-examples-note";
  text(id, `${examples.length} / 6 组 · ${count} / 6000 字${count > 6000 ? " · 超出限制，请缩短后保存" : ""}`);
  tone(id, count > 6000 ? "error" : "neutral");
}
function renderDialogueExamples(defaults) {
  const parent = $(defaults ? "proactive-default-examples" : "proactive-examples");
  const owner = exampleOwner(defaults);
  const examples = owner?.[defaults ? "default_examples" : "examples"] || [];
  const groupId = proactiveSelected;
  parent.replaceChildren();
  examples.forEach((turns, index) => {
    const card = document.createElement("fieldset"); card.className = "dialogue-example";
    const legend = document.createElement("legend"); legend.textContent = `示例 ${index + 1}`; card.append(legend);
    turns.forEach((turn, turnIndex) => {
      const label = document.createElement("label"); label.textContent = `${turnIndex % 2 ? "机器人" : "群友"} · 第 ${Math.floor(turnIndex / 2) + 1} 轮`;
      const input = document.createElement("textarea"); input.rows = 2; input.value = turn;
      input.dataset.proactiveEdit = ""; input.dataset.exampleIndex = String(index); input.dataset.exampleTurn = String(turnIndex);
      input.addEventListener("input", () => changeDialogueExamples(defaults, list => { list[index][turnIndex] = input.value; }, groupId, false));
      label.append(input); card.append(label);
    });
    const actions = document.createElement("div"); actions.className = "form-actions";
    const button = (caption, action) => {
      const element = document.createElement("button"); element.type = "button"; element.className = "secondary small";
      element.textContent = caption; element.dataset.proactiveEdit = "";
      element.addEventListener("click", () => changeDialogueExamples(defaults, action, groupId)); actions.append(element);
    };
    if (turns.length < 12) button("添加一轮对话", list => list[index].push("", ""));
    if (turns.length > 2) button("删除最后一轮", list => list[index].splice(-2));
    button("删除这组示例", list => list.splice(index, 1));
    card.append(actions); parent.append(card);
  });
  renderExamplesCount(defaults);
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
  renderSettingsState(state);
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
$("proactive-add-example").addEventListener("click", () => {
  if ((exampleOwner(false)?.examples || []).length < 6) changeDialogueExamples(false, list => list.push(["", ""]));
});
$("proactive-add-default-example").addEventListener("click", () => {
  if ((exampleOwner(true)?.default_examples || []).length < 6) changeDialogueExamples(true, list => list.push(["", ""]));
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
  proactiveDraft.default_examples = [];
  $("proactive-default-style").value = proactiveBuiltinStyle;
  renderDialogueExamples(true);
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
$("proactive-clear-memory").addEventListener("click", async () => {
  if ($("proactive-clear-memory").disabled || !authenticated || proactiveAction) return;
  const group = proactiveSelected;
  const name = proactiveGroups.find(item => item.group_id === group)?.name || "所选群";
  const confirmation = window.prompt(`即将永久清空「${name}」（${group}）的全部记忆，从零开始。性格和群设置保留。\n请输入群号 ${group} 确认：`);
  if (confirmation === null) return;
  if (confirmation.trim() !== group) {
    memoryResetFeedback = {group, message: "群号不一致，未清空任何记忆。", tone: "error"};
    renderProactiveEditor();
    return;
  }
  const epoch = authEpoch;
  const key = `qunling-memory-reset:${group}`;
  let requestId = sessionStorage.getItem(key);
  if (!/^[a-f0-9]{32}$/.test(requestId || "")) {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    requestId = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
    sessionStorage.setItem(key, requestId);
  }
  proactiveAction = memoryResetAction = true;
  memoryResetFeedback = {group, message: "正在清空，请稍候…", tone: "neutral"};
  renderProactiveEditor();
  controls();
  try {
    const result = await api("/api/proactive/memory/reset", {group_id: group, confirmation: group, request_id: requestId}, 60000);
    if (epoch !== authEpoch || !authenticated) return;
    if (result.cleared !== true || result.group_id !== group) throw Error("未确认清空结果，请再次点击清空核对；重复核对不会再次删除新的记忆。");
    sessionStorage.removeItem(key);
    memoryResetFeedback = {group, message: `「${name}」（${group}）的记忆已清空，之后将从新的对话开始。`, tone: "success"};
    clearMemoryView();
  } catch (error) {
    if (epoch !== authEpoch || !authenticated) return;
    memoryResetFeedback = {group, message: `${error.message} 如未确认结果，可再次点击清空核对。`, tone: "error"};
  } finally {
    proactiveAction = memoryResetAction = false;
    renderProactiveEditor();
    controls();
  }
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
  for (const [label, samples] of [["默认语气", config.default_examples], ...Object.entries(config.groups).map(([id, group]) => [`群 ${id}`, group.examples])]) {
    const error = exampleValidationError(samples);
    if (error) { proactiveFeedback(`${label}：${error} 输入内容已保留。`); return; }
  }
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
    proactiveFeedback("群聊设置已保存。性格设置对各种回复统一生效，主动参与只在已开启的群生效。", "success");
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
  const epoch = authEpoch;
  try {
    // Read disabled fields explicitly: the form is locked during application,
    // and native FormData would omit them once controls() disables the inputs.
    const payload = {};
    for (const key of [...aiDraftFields, ...aiSecretFields]) if (form.elements[key]) payload[key] = form.elements[key].value;
    payload.features = featureInputs().filter(field => field.checked).map(field => field.value);
    if (settingsRevision) payload.settings_revision = settingsRevision;
    await api("/api/configure", payload);
    if (epoch !== authEpoch || !authenticated) return;
    settingsPending = true;
    text("form-message", "配置已提交，正在应用；完成后会显示结果。");
    tone("form-message", "neutral");
  } catch (error) {
    if (epoch !== authEpoch) return;
    if (error.status === 409) settingsConflict = true;
    text("form-message", error.message);
    tone("form-message", "error");
  } finally {
    await poll(true);
    configAction = false;
    // Only the status request started after this POST may finish application.
    // Failed/auth-invalidated polls clear setupState, keeping the draft pending.
    if (epoch === authEpoch && authenticated && setupState) renderSettingsState(setupState);
    persistSettingsDraft();
    controls();
  }
});
function editSettings() {
  if (!authenticated || configAction || settingsPending || settingsReloading || setupState?.stage === "configuring") return;
  settingsDirty = true;
  persistSettingsDraft();
  controls();
}
$("settings").addEventListener("input", editSettings);
$("settings").addEventListener("change", editSettings);
$("settings-reload").addEventListener("click", async () => {
  if ($("settings-reload").disabled) return;
  if (settingsDirty && !window.confirm("重新载入会丢弃未保存的 AI 与工具修改，改用服务器配置。继续吗？")) return;
  const epoch = authEpoch;
  settingsReloading = true;
  controls();
  try {
    const state = await api("/api/status");
    if (epoch !== authEpoch || !authenticated) return;
    setupState = state;
    hydrateSettings(state);
    persistSettingsDraft();
    text("form-message", "已重新载入服务器配置。");
    tone("form-message", "success");
  } catch (error) { text("form-message", error.message); tone("form-message", "error"); }
  finally { settingsReloading = false; controls(); }
});
document.querySelectorAll("[data-group-tab]").forEach(button => button.addEventListener("click", () => setGroupTab(button.dataset.groupTab)));
window.addEventListener?.("beforeunload", event => {
  if (!(settingsDirty || proactiveDirty || settingsPending || configAction || proactiveAction || memoryDirty || memoryBusy)) return;
  event.preventDefault();
  event.returnValue = "";
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
  if (token !== value) { writeDraft("ai", null); writeDraft("groups", null); }
  token = value;
  $("token").value = "";
  loaded = false;
  settingsDirty = settingsConflict = settingsPending = settingsDraftRestored = groupDraftRestored = false;
  settingsRevision = null;
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
const consolePages = {
  overview: ["工作台", "运行状态与日常管理，一眼找到。"],
  models: ["AI 服务", "统一管理模型、图片与联网搜索参数。"],
  groups: ["群聊管理", "管理每个群的性格、参与方式和记忆。"],
  connection: ["QQ 连接", "查看在线状态，管理扫码登录。"],
  requests: ["任务中心", "查看消息、文件任务和自由对话的进度与结果。"],
  knowledge: ["资料知识库", "管理公开资料复用、订阅来源和采集状态。"],
  maintenance: ["版本维护", "检查更新、切换版本与查看维护记录。"],
  advanced: ["高级管理", "前往 LangBot 管理其他插件与流水线参数。"]
};
function selectNav(id, focus = false) {
  id = id === "proactive" ? "groups" : Object.hasOwn(consolePages, id) ? id : "overview";
  document.querySelectorAll("[data-page]").forEach(page => { page.hidden = page.dataset.page !== id; });
  text("page-title", consolePages[id][0]);
  text("page-description", consolePages[id][1]);
  document.title = `QunLing 群灵 · ${consolePages[id][0]}`;
  navLinks.forEach((link) => {
    const active = link.getAttribute("href") === "#" + id;
    link.classList.toggle("is-active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (focus) {
    window.scrollTo?.({top: 0, left: 0, behavior: "instant"});
    $("page-title").focus?.({preventScroll: true});
  }
}
window.addEventListener?.("hashchange", () => selectNav(location.hash.slice(1), true));
selectNav(location.hash.slice(1));
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

function clearMemoryView() {
  memorySequence += 1;
  memorySelected = null; memoryDirty = false; memoryGroup = ""; memoryOffset = 0; memoryNext = null;
  $("memory-list").replaceChildren(); $("memory-editor").hidden = true;
  for (const id of ["memory-name", "memory-traits", "memory-preferences", "memory-notes", "memory-content"]) $(id).value = "";
  $("memory-prev").hidden = $("memory-next").hidden = true;
  text("memory-note", "点击查询读取本群记忆。");
}
function leaveMemory() {
  if (memoryBusy || memoryDirty && !window.confirm("这条记忆的修改尚未保存，放弃修改并离开？")) return false;
  clearMemoryView();
  return true;
}
function memoryControls() {
  for (const id of ["memory-save", "memory-delete", "memory-cancel", "memory-name", "memory-traits", "memory-preferences", "memory-notes", "memory-content", "memory-refresh", "memory-prev", "memory-next", "memory-export", "memory-kind", "memory-query"]) {
    $(id).disabled = memoryBusy || !authenticated;
  }
  $("memory-save").disabled ||= !memoryDirty;
}
function openMemory(item) {
  if (memoryBusy || memoryDirty && !window.confirm("放弃当前记忆的未保存修改，打开另一条？")) return;
  memorySelected = {...item, group: proactiveSelected}; memoryDirty = false;
  $("memory-editor").hidden = false;
  text("memory-editor-title", item.kind === "profile" ? "编辑「" + item.title + "」" : "编辑长期事件记忆");
  $("memory-profile-fields").hidden = item.kind !== "profile";
  $("memory-episode-fields").hidden = item.kind !== "episode";
  if (item.kind === "profile") {
    for (const [id, field] of [["memory-name", "name"], ["memory-traits", "traits"], ["memory-preferences", "preferences"], ["memory-notes", "notes"]])
      $(id).value = Array.isArray(item.value[field]) ? item.value[field].join("\n") : item.value[field] || "";
  } else $("memory-content").value = item.value;
  text("memory-editor-message", "修改只影响这条记忆。保存前会检查它是否被更新。"); tone("memory-editor-message", "neutral");
  memoryControls();
  $("memory-editor").scrollIntoView?.({block: "nearest", behavior: "smooth"});
}
async function loadMemory(offset = 0) {
  if (!authenticated || !proactiveSelected || memoryBusy) return;
  const group = proactiveSelected, epoch = authEpoch, sequence = ++memorySequence;
  if (memoryGroup !== group) { memorySelected = null; memoryDirty = false; $("memory-editor").hidden = true; }
  memoryGroup = group; memoryOffset = offset;
  text("memory-note", "正在读取本群记忆…");
  $("memory-list").replaceChildren(); $("memory-prev").hidden = $("memory-next").hidden = true;
  try {
    const data = await api("/api/memory", {group_id: group, action: "list", offset, query: $("memory-query").value.trim(), kind: $("memory-kind").value || "all"}, 60000);
    if (!authenticated || epoch !== authEpoch || group !== proactiveSelected || sequence !== memorySequence) return;
    for (const item of data.items || []) {
      const row = requestNode("article", ""); row.className = "memory-row";
      const button = requestNode("button", (item.kind === "profile" ? "资料 · " : "事件 · ") + item.title);
      button.type = "button"; button.className = "memory-open"; button.addEventListener("click", () => openMemory(item));
      const preview = item.kind === "profile" ? [item.value.name, ...(item.value.traits || []), ...(item.value.preferences || []), item.value.notes].filter(Boolean).join(" · ") : item.value;
      const status = {active: "使用中", superseded: "已被新记忆替代", archived: "已归档", deleted: "已停用"};
      row.append(button, requestNode("p", preview.slice(0, 240)), requestNode("small", [item.updated_at, status[item.status]].filter(Boolean).join(" · ")));
      $("memory-list").append(row);
    }
    memoryNext = data.next_offset ?? null;
    $("memory-prev").hidden = offset === 0; $("memory-next").hidden = memoryNext === null;
    text("memory-note", data.total ? `共 ${data.total} 条匹配记忆 · 第 ${Math.floor(offset / 30) + 1} 页 · 点击记录编辑` : "暂无匹配记忆。可以更换关键词或类型；聊天上下文不在此列表中。");
  } catch (error) {
    if (authenticated && epoch === authEpoch && group === proactiveSelected && sequence === memorySequence) text("memory-note", error.message);
  }
}
async function changeMemory(action) {
  const item = memorySelected;
  if (!authenticated || !item || memoryBusy || item.group !== proactiveSelected) return;
  if (action === "delete" && !window.confirm(`删除「${item.title}」这条记忆？此操作不可撤销。`)) return;
  const epoch = authEpoch;
  const body = {group_id: item.group, action, kind: item.kind, revision: item.revision};
  if (item.kind === "profile") body.sender_id = item.sender_id;
  else { body.collection_id = item.collection_id; body.id = item.id; }
  if (action === "save") body.value = item.kind === "profile" ? {
    name: $("memory-name").value, notes: $("memory-notes").value,
    traits: $("memory-traits").value.split("\n").map(value => value.trim()).filter(Boolean),
    preferences: $("memory-preferences").value.split("\n").map(value => value.trim()).filter(Boolean)
  } : $("memory-content").value;
  memoryBusy = proactiveAction = true; controls(); memoryControls();
  text("memory-editor-message", action === "save" ? "正在保存这条记忆…" : "正在删除这条记忆…");
  let success = false;
  try {
    const result = await api("/api/memory", body, 60000);
    if (!authenticated || epoch !== authEpoch) return;
    if (result[action === "save" ? "saved" : "deleted"] !== true || result.group_id !== item.group) throw Error("未确认操作结果，请刷新核对。草稿仍保留。");
    success = true; memoryDirty = false; memorySelected = null; $("memory-editor").hidden = true;
  } catch (error) {
    if (authenticated && epoch === authEpoch) { text("memory-editor-message", error.message + " 草稿已保留，可关闭编辑后刷新核对。"); tone("memory-editor-message", "error"); }
  } finally {
    memoryBusy = proactiveAction = false; controls(); memoryControls();
    if (success) { await loadMemory(0); text("memory-note", (action === "save" ? "已保存。" : "已删除。") + $("memory-note").textContent); }
  }
}
async function exportMemory() {
  if (!authenticated || !proactiveSelected || memoryBusy) return;
  const group = proactiveSelected, epoch = authEpoch;
  memoryBusy = true; memoryControls(); text("memory-note", "正在导出本群已保存的记忆…");
  try {
    const data = await api("/api/memory", {group_id: group, action: "export"}, 60000);
    if (!authenticated || epoch !== authEpoch || group !== proactiveSelected) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json;charset=utf-8"}));
    const link = document.createElement("a"); link.href = url; link.download = `qunling-memory-${group}-${new Date().toISOString().slice(0, 10)}.json`;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    text("memory-note", `已导出 ${data.count} 条记忆。文件包含本群资料和长期事件，不含性格设置或 QQ 历史消息。`);
  } catch (error) { if (authenticated && epoch === authEpoch && group === proactiveSelected) text("memory-note", error.message); }
  finally { memoryBusy = false; memoryControls(); }
}
$("memory-refresh").addEventListener("click", () => loadMemory());
$("memory-kind").addEventListener("change", () => loadMemory());
$("memory-query").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); loadMemory(); } });
$("memory-name").addEventListener("keydown", event => { if (event.key === "Enter") event.preventDefault(); });
$("memory-prev").addEventListener("click", () => loadMemory(Math.max(0, memoryOffset - 30)));
$("memory-next").addEventListener("click", () => { if (memoryNext !== null) loadMemory(memoryNext); });
$("memory-save").addEventListener("click", () => changeMemory("save"));
$("memory-delete").addEventListener("click", () => changeMemory("delete"));
$("memory-cancel").addEventListener("click", () => { if (leaveMemory()) loadMemory(); });
$("memory-export").addEventListener("click", exportMemory);
for (const id of ["memory-name", "memory-traits", "memory-preferences", "memory-notes", "memory-content"]) $(id).addEventListener("input", () => { memoryDirty = true; memoryControls(); });

const taskStatuses = {pending:"排队中", running:"处理中", waiting:"等待继续", partial:"部分完成", completed:"处理完成", failed:"处理失败", cancelled:"已取消"};
const deliveryStatuses = {not_scheduled:"未安排自动发送", queued:"等待发送", claimed:"等待发送", started:"正在发送", sent:"QQ 发送已确认", unconfirmed:"QQ 发送未确认", cancelled:"发送已取消", failed:"发送失败", expired:"发送已过期"};
const taskErrors = {network:"网络连接异常", timeout:"处理超时", rate_limit:"服务限流", service:"服务不可用", invalid_input:"输入无效", permission:"权限失效", needs_input:"等待补充信息", delivery_unconfirmed:"发送结果未确认", budget:"超出任务限额", cancelled:"任务取消", internal:"处理异常"};
function clearTasks() {
  tasksSequence += 1; tasksCursor = null; tasksBusy = false; tasksOlder = false;
  for (const id of ["tasks-list", "tasks-counts", "tasks-live-list"]) $(id).replaceChildren();
  $("tasks-older").hidden = true;
  text("tasks-note", ""); text("tasks-live-note", "");
}
function setTaskTab(tab) {
  taskTab = tab;
  for (const name of ["requests", "files", "live"]) {
    $("task-panel-" + name).hidden = name !== tab;
    $("task-tab-" + name).setAttribute("aria-pressed", String(name === tab));
  }
  if (tab === "files") loadTasks();
  if (tab === "live") renderLiveTasks();
}
async function loadTasks(older = false, automatic = false) {
  if (!authenticated || automatic && (tasksBusy || tasksOlder)) return;
  const cursor = older ? tasksCursor : null;
  if (older && !cursor) return;
  const sequence = ++tasksSequence, epoch = authEpoch;
  tasksBusy = true; tasksOlder = Boolean(cursor); $("tasks-refresh").disabled = true;
  const filters = {limit:30};
  if ($("tasks-group").value.trim()) filters.group = $("tasks-group").value.trim();
  if ($("tasks-status").value) filters.status = $("tasks-status").value;
  if (cursor) filters.before = cursor;
  text("tasks-note", "正在读取后台任务…");
  try {
    const data = await api("/api/tasks?" + new URLSearchParams(filters));
    if (!authenticated || epoch !== authEpoch || sequence !== tasksSequence) return;
    $("tasks-list").replaceChildren(); $("tasks-counts").replaceChildren();
    for (const [label, count] of [["排队 / 处理", (data.counts?.pending || 0) + (data.counts?.running || 0)], ["等待 / 部分完成", (data.counts?.waiting || 0) + (data.counts?.partial || 0)], ["处理完成", data.counts?.completed || 0], ["失败 / 取消", (data.counts?.failed || 0) + (data.counts?.cancelled || 0)]]) {
      const card = requestNode("div", ""); card.append(requestNode("span", label), requestNode("strong", String(count))); $("tasks-counts").append(card);
    }
    for (const item of data.tasks || []) {
      const row = requestNode("article", ""); row.className = "request-row";
      const conversation = item.conversation?.startsWith("group:") ? "群 " + item.conversation.slice(6) : "私聊 " + (item.actor || "");
      row.append(requestNode("h3", (taskStatuses[item.status] || "未知状态") + " · " + conversation));
      const delivery = requestNode("span", deliveryStatuses[item.delivery] || "发送状态未知"); delivery.className = "phase";
      delivery.dataset.tone = item.delivery === "sent" ? "success" : ["unconfirmed", "failed", "expired"].includes(item.delivery) ? "warning" : "neutral";
      row.append(delivery, requestNode("p", `${requestTime(item.created_at)} · 发起人 ${item.actor} · 耗时 ${item.elapsed_seconds} 秒 · 已执行 ${item.attempts} 次`));
      if (item.error_code) row.append(requestNode("p", taskErrors[item.error_code] || "任务异常，请核对处理记录。"));
      if (item.delivery_error && item.delivery_error !== item.error_code) row.append(requestNode("p", "交付：" + (taskErrors[item.delivery_error] || "发送环节未完成，请核对 QQ 中的实际结果。")));
      if (item.retry_at) row.append(requestNode("small", "下一次执行时间：" + requestTime(item.retry_at)));
      row.append(requestNode("small", `任务 ${item.id} · 已产生 ${item.result_count} 个结果`));
      $("tasks-list").append(row);
    }
    tasksCursor = data.next_before || null; $("tasks-older").hidden = !tasksCursor;
    text("tasks-note", (data.tasks?.length ? "统计按所选群汇总，不受状态筛选影响。" : "当前筛选下暂无后台任务。") + "采样于 " + requestTime(data.sampled_at) + (cursor ? " · 较早记录，刷新返回最新。" : ""));
  } catch (error) {
    if (authenticated && epoch === authEpoch && sequence === tasksSequence) {
      $("tasks-list").replaceChildren(); $("tasks-counts").replaceChildren(); $("tasks-older").hidden = true; tasksCursor = null;
      text("tasks-note", error.message);
    }
  } finally { if (sequence === tasksSequence) { tasksBusy = false; $("tasks-refresh").disabled = false; } }
}
function renderLiveTasks() {
  $("tasks-live-list").replaceChildren();
  if (!authenticated || !proactiveConfigReady || !proactiveStatus?.available) { text("tasks-live-note", "当前状态暂不可用，请刷新。不会将旧状态当作正在运行。"); return; }
  const entries = Object.entries(proactiveStatus.groups || {});
  for (const [group, value] of entries) {
    if ((proactiveSavedConfig?.groups?.[group]?.mode || "off") === "off") continue;
    const row = requestNode("article", ""); row.className = "request-row";
    const name = proactiveGroups.find(item => item.group_id === group)?.name || group;
    const code = !proactiveSavedConfig?.enabled ? "off" : !setupState?.qq_connected ? "disconnected" : value.status_code;
    row.append(requestNode("h3", name + " · " + (proactiveRuntimeStates[code]?.[0] || "未知状态")));
    row.append(requestNode("p", `群 ${group} · ${value.active_conversations || 0} 路接话 · ${value.queued_conversations || 0} 路等待 · ${value.explicit_requests || 0} 个明确任务`));
    if (value.last_event) row.append(requestNode("small", (proactiveEventNames[value.last_event.code] || "最近处理") + " · " + proactiveTime(value.last_event.at)));
    $("tasks-live-list").append(row);
  }
  text("tasks-live-note", ($("tasks-live-list").children.length ? "状态采样：" : "暂无已配置参与的群。状态采样：") + proactiveTime(proactiveStatus.generated_at));
}
for (const tab of ["requests", "files", "live"]) $("task-tab-" + tab).addEventListener("click", () => setTaskTab(tab));
$("tasks-refresh").addEventListener("click", () => loadTasks());
$("tasks-older").addEventListener("click", () => loadTasks(true));
for (const id of ["tasks-group", "tasks-status"]) $(id).addEventListener("change", () => loadTasks());
$("tasks-live-refresh").addEventListener("click", async () => { await pollProactive(authEpoch, true); renderLiveTasks(); });
setInterval(() => {
  if (!authenticated || $("requests").hidden) return;
  if (taskTab === "files") loadTasks(false, true);
  if (taskTab === "live") renderLiveTasks();
}, 10000);
poll();
setInterval(poll, 4000);
setInterval(expireQQStatus, 1000);
