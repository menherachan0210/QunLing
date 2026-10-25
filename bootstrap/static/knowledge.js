// Keep this optional panel independent from QQ connection and onboarding state.
(() => {
  let revision = null, busy = false;
  resetKnowledge = () => {
    revision = null;
    $("knowledge-sources").value = "";
    $("knowledge-status").replaceChildren();
    $("knowledge-save").disabled = true;
    text("knowledge-note", "连接后刷新知识库设置与状态。");
  };
  function status(data) {
    const counters = data.status?.counters || {};
    text("knowledge-note", `本地命中 ${counters.hit || 0} 次 · 未命中 ${counters.miss || 0} 次 · 实时读取 ${counters.live || 0} 次 · 采集成功 ${counters.collected || 0} 次 · 采集失败 ${counters.failed || 0} 次。统计自启用以来累计，命中不代表资料足以回答。`);
    const list = $("knowledge-status");
    list.replaceChildren();
    for (const row of Object.values(data.status?.sources || {})) {
      const item = document.createElement("li");
      item.textContent = `群 ${row.group_id || "—"} · ${row.url || "来源"} · ${row.status === "ok" ? "采集成功" : row.status === "running" ? "采集中" : "采集失败"} · 上次尝试 ${new Date(row.checked * 1000).toLocaleString()} · 下次计划 ${new Date(row.next_at * 1000).toLocaleString()}`;
      list.append(item);
    }
  }
  async function load() {
    if (!authenticated || busy) return;
    const epoch = authEpoch;
    busy = true;
    $("knowledge-save").disabled = true;
    try {
      const data = await api("/api/knowledge");
      if (!authenticated || epoch !== authEpoch) return;
      const config = data.config;
      revision = config.revision;
      $("knowledge-enabled").checked = config.enabled;
      $("knowledge-search-ttl").value = config.search_ttl;
      $("knowledge-page-ttl").value = config.page_ttl;
      $("knowledge-budget").value = config.daily_budget;
      $("knowledge-sources").value = config.sources.map(row => `${row.group_id} | ${row.url} | ${row.interval / 60} | ${row.ttl / 60}`).join("\n");
      status(data);
    } catch (error) {
      revision = null;
      if (authenticated && epoch === authEpoch) text("knowledge-note", error.message);
    } finally {
      busy = false;
      $("knowledge-save").disabled = revision === null || !authenticated || epoch !== authEpoch;
    }
  }
  $("knowledge-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!authenticated || busy || revision === null) return;
    busy = true;
    const epoch = authEpoch;
    $("knowledge-save").disabled = true;
    try {
      const sources = $("knowledge-sources").value.split("\n").filter(row => row.trim()).map(row => {
        const columns = row.split("|").map(value => value.trim());
        if (columns.length !== 4) throw Error("每行需填写：群号 | URL | 刷新分钟 | 有效分钟。");
        return {group_id:columns[0], url:columns[1], interval:Number(columns[2])*60, ttl:Number(columns[3])*60};
      });
      const data = await api("/api/knowledge", {revision, enabled:$("knowledge-enabled").checked,
        search_ttl:Number($("knowledge-search-ttl").value), page_ttl:Number($("knowledge-page-ttl").value),
        daily_budget:Number($("knowledge-budget").value), sources});
      if (!authenticated || epoch !== authEpoch) return;
      revision = data.config.revision;
      status(data);
      text("knowledge-note", "知识库设置已保存，采集会在下一轮检查时按计划开始。实时核实仍会联网。");
    } catch (error) {
      if (authenticated && epoch === authEpoch) text("knowledge-note", error.message);
    } finally {
      busy = false;
      $("knowledge-save").disabled = !authenticated || epoch !== authEpoch;
    }
  });
  $("knowledge-open").addEventListener("toggle", () => { if ($("knowledge-open").open) load(); });
  $("knowledge-refresh").addEventListener("click", load);
})();
