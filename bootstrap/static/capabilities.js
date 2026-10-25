(() => {
  let busy = false, timer = null;
  const names = {chat:"聊天与工具", vision:"视觉理解", search:"联网搜索", image:"图片生成"};
  const states = {passed:"通过", failed:"未通过", unconfirmed:"结果未确认"};
  resetCapabilities = () => {
    clearTimeout(timer);
    busy = false;
    $("capabilities-cost").checked = false;
    $("capabilities-results").replaceChildren();
    $("capabilities-start").disabled = true;
    text("capabilities-note", "连接后可查看已有测试结果，不会自动重试调用。");
  };
  function render(job) {
    busy = job?.status === "running";
    $("capabilities-start").disabled = busy || !authenticated;
    $("capabilities-results").replaceChildren();
    for (const row of job?.results || []) {
      const item = document.createElement("li");
      item.textContent = `${names[row.check] || "测试"} · ${states[row.status] || "未知"} · ${row.seconds} 秒：${row.message}`;
      $("capabilities-results").append(item);
    }
    text("capabilities-note", !job ? "本次服务启动后尚无测试结果。" : busy ? "正在逐项测试，请等待；不会自动重复计费调用。" : "测试已结束。结果针对测试开始时保存的配置，不代表所有任务或未来请求都能成功。");
  }
  async function refresh() {
    if (!authenticated) return;
    clearTimeout(timer);
    const epoch = authEpoch;
    try {
      const data = await api("/api/capabilities");
      if (!authenticated || epoch !== authEpoch) return;
      render(data.job);
      if (busy) timer = setTimeout(refresh, 2500);
    } catch (error) {
      if (authenticated && epoch === authEpoch) text("capabilities-note", "无法确认测试状态，请点查看结果；不要重复提交可能已计费的调用。");
    }
  }
  $("capabilities-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!authenticated || busy || !$("capabilities-cost").checked) return;
    const checks = [...$("capabilities-form").querySelectorAll('input[name="check"]:checked')].map(input => input.value);
    if (!checks.length) { text("capabilities-note", "请至少选择一项。"); return; }
    const epoch = authEpoch;
    busy = true;
    $("capabilities-start").disabled = true;
    // getRandomValues also works on HTTP consoles reached over a local network.
    const requestId = [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, "0")).join("");
    try {
      const job = await api("/api/capabilities", {checks, acknowledge_cost:true, request_id:requestId});
      if (!authenticated || epoch !== authEpoch) return;
      $("capabilities-cost").checked = false;
      render(job);
      timer = setTimeout(refresh, 1000);
    } catch (error) {
      if (authenticated && epoch === authEpoch) text("capabilities-note", error.message + " 请查看结果确认状态；没有自动重试。");
    }
  });
  $("capabilities-open").addEventListener("toggle", () => { if ($("capabilities-open").open) refresh(); });
  $("capabilities-refresh").addEventListener("click", refresh);
})();
