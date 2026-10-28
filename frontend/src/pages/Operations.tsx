import { Advanced, Components, Storage } from "./Advanced";
import React, { useEffect, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronRight,
  Download,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { api, client, errorText, refresh, time, useData } from "../api";
import {
  Badge,
  Button,
  Drawer,
  Empty,
  Field,
  Loading,
  Notice,
  PageTitle,
  SaveBar,
  SearchField,
  Section,
  Toggle,
  useAction,
  useDraft,
} from "../ui";
const statuses: any = {
  active: "处理中",
  running: "运行中",
  completed: "已完成",
  processed: "流程结束",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  unconfirmed: "结果未确认",
  rejected: "未能排队",
  pending: "排队中",
  partial: "部分完成",
  waiting: "等待中",
  retry_wait: "等待重试",
};
const phases: any = {
  received: "收到消息",
  queued: "等待处理",
  preparing: "准备上下文",
  runtime_wait: "等待组件",
  running: "处理消息",
  model: "生成回复",
  tool: "执行工具",
  tool_wait: "等待工具",
  retry_wait: "准备重试",
  resuming: "继续回复",
  sending: "发送回复",
  delivered: "发送已确认",
  ended: "处理结束",
};
export function Records({ active }: { active: boolean }) {
  const [tab, setTab] = useState("requests"),
    [group, setGroup] = useState(""),
    [status, setStatus] = useState(""),
    [cursor, setCursor] = useState(""),
    [previous, setPrevious] = useState<string[]>([]),
    [selected, setSelected] = useState<any>(null);
  const params = new URLSearchParams({
    limit: "30",
    ...(group ? { group } : {}),
    ...(status ? { status } : {}),
    ...(cursor ? { before: cursor } : {}),
  });
  const records = useData(
    "/api/" + tab + "?" + params,
    cursor ? 0 : 10000,
    active,
  );
  const groups = useData("/api/proactive/groups", 0, active);
  const details = useData(
    selected && tab === "requests"
      ? "/api/requests?request_id=" + selected.request_id
      : "/api/tasks?task_id=" + (selected?.id || ""),
    0,
    active && !!selected,
  );
  const rows = records.data?.[tab] || [];
  const change = (fn: () => void) => {
    fn();
    setCursor("");
    setPrevious([]);
    setSelected(null);
  };
  return (
    <>
      <PageTitle
        title="任务与记录"
        description="区分处理完成与发送成功，找到实际卡住的环节。"
      >
        <Button onClick={() => records.refetch()}>
          <RefreshCw size={15} />
          刷新
        </Button>
      </PageTitle>
      <div className="tabs">
        {[
          ["requests", "消息请求"],
          ["tasks", "后台文件任务"],
        ].map(([id, label]) => (
          <button
            key={id}
            aria-pressed={tab === id}
            onClick={() =>
              change(() => {
                setTab(id);
                setStatus("");
              })
            }
          >
            {label}
          </button>
        ))}
      </div>
      <div className="filter-row">
        <select
          aria-label="筛选群聊"
          value={group}
          onChange={(e) => change(() => setGroup(e.target.value))}
        >
          <option value="">全部群聊</option>
          {groups.data?.groups?.map((g: any) => (
            <option key={g.group_id} value={g.group_id}>
              {g.name || g.group_id}
            </option>
          ))}
        </select>
        <select
          aria-label="筛选状态"
          value={status}
          onChange={(e) => change(() => setStatus(e.target.value))}
        >
          <option value="">全部状态</option>
          {(tab === "requests"
            ? [
                "active",
                "completed",
                "processed",
                "failed",
                "cancelled",
                "interrupted",
                "unconfirmed",
                "rejected",
              ]
            : [
                "pending",
                "running",
                "waiting",
                "partial",
                "completed",
                "failed",
                "cancelled",
              ]
          ).map((key) => (
            <option key={key} value={key}>
              {statuses[key]}
            </option>
          ))}
        </select>
        <span className="filter-spacer" />
        <span className="muted">
          {cursor ? "正在查看历史页" : "最新记录自动更新"}
        </span>
      </div>
      {records.isPending ? (
        <Loading />
      ) : records.error ? (
        <Notice>{errorText(records.error)}</Notice>
      ) : !rows.length ? (
        <Empty
          title="暂无匹配记录"
          description="可以调整筛选条件，或等待下一条消息。"
        />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>状态</th>
                <th>群聊 / 会话</th>
                <th>处理结果</th>
                <th>时间</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r: any) => (
                <tr
                  key={r.request_id || r.id}
                  onClick={() => setSelected(r)}
                  tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && setSelected(r)}
                >
                  <td>
                    <Badge
                      tone={
                        r.status === "failed"
                          ? "error"
                          : r.status === "completed"
                            ? "success"
                            : "neutral"
                      }
                    >
                      {statuses[r.status] || r.status}
                    </Badge>
                  </td>
                  <td>
                    <strong>
                      {r.group_id || r.conversation || r.launcher_id || "—"}
                    </strong>
                    <small>
                      {r.actor ||
                        r.sender ||
                        r.sender_id ||
                        r.request_id?.slice(0, 12)}
                    </small>
                  </td>
                  <td>
                    {tab === "tasks"
                      ? r.delivery === "sent"
                        ? "文件已发送"
                        : r.delivery === "unconfirmed"
                          ? "发送结果未确认"
                          : "交付：" + (r.delivery || "尚未调度")
                      : phases[r.phase] || r.reason || "查看处理详情"}
                  </td>
                  <td>
                    <time>
                      {time(r.created || r.created_at || r.started_at)}
                    </time>
                  </td>
                  <td>
                    <ChevronRight size={16} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="pagination">
        <small>{rows.length} 条记录</small>
        <Button
          disabled={!previous.length}
          onClick={() => {
            setCursor(previous.at(-1) || "");
            setPrevious(previous.slice(0, -1));
          }}
        >
          <ArrowLeft size={14} />
          较新
        </Button>
        <Button
          disabled={!records.data?.next_before && !records.data?.next_cursor}
          onClick={() => {
            setPrevious([...previous, cursor]);
            setCursor(records.data.next_before || records.data.next_cursor);
          }}
        >
          较早
          <ArrowRight size={14} />
        </Button>
      </div>
      <Drawer
        open={!!selected}
        onOpenChange={(v) => !v && setSelected(null)}
        title={tab === "requests" ? "请求经过" : "文件任务详情"}
        description={selected?.request_id || selected?.id}
      >
        {details.isPending ? (
          <Loading />
        ) : details.error ? (
          <Notice>{errorText(details.error)}</Notice>
        ) : (
          <>
            <Badge tone={selected?.status === "failed" ? "error" : "neutral"}>
              {statuses[selected?.status] || selected?.status}
            </Badge>
            {tab === "requests" ? (
              <ol className="timeline">
                {(
                  details.data?.request?.events ||
                  details.data?.events ||
                  details.data?.requests?.[0]?.events ||
                  []
                ).map((ev: any, i: number) => (
                  <li key={i}>
                    <i />
                    <div>
                      <strong>
                        {phases[ev.phase] ||
                          phases[ev.event] ||
                          ev.code ||
                          ev.phase ||
                          "处理事件"}
                      </strong>
                      <small>{time(ev.at || ev.timestamp)}</small>
                      {ev.reason && <p>{ev.reason}</p>}
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="detail-rows">
                {Object.entries(details.data?.tasks?.[0] || selected || {})
                  .filter(([k, v]) =>
                    [
                      "status",
                      "delivery",
                      "attempts",
                      "result_count",
                      "error_code",
                      "elapsed_seconds",
                    ].includes(k),
                  )
                  .map(([key, value]) => (
                    <div key={key}>
                      <span>
                        {
                          (
                            {
                              status: "处理状态",
                              delivery: "交付状态",
                              attempts: "已执行次数",
                              result_count: "结果数量",
                              error_code: "错误代码",
                              elapsed_seconds: "耗时（秒）",
                            } as any
                          )[key]
                        }
                      </span>
                      <strong>{String(value ?? "—")}</strong>
                    </div>
                  ))}
              </div>
            )}
          </>
        )}
      </Drawer>
    </>
  );
}
export function Knowledge({ active }: { active: boolean }) {
  const remote = useData("/api/knowledge", 15000, active),
    groups = useData("/api/proactive/groups", 0, active),
    draft = useDraft("knowledge", remote.data?.config),
    action = useAction();
  const v = draft.value;
  const update = (key: string, value: any) =>
    draft.setValue({ ...v, [key]: value });
  return (
    <>
      <PageTitle
        title="资料与知识"
        description="管理可复用的公开资料，按群设置订阅来源。"
      />
      <Notice>{remote.error && errorText(remote.error)}</Notice>
      {!v ? (
        <Loading />
      ) : (
        <>
          <Section title="资料复用">
            <Toggle
              label="启用知识复用"
              description="优先复用仍有效的资料，需要实时核实时继续联网。"
              checked={v.enabled}
              onChange={(value) => update("enabled", value)}
            />
            <div className="form-grid three">
              <Field label="搜索结果有效期（秒）">
                <input
                  type="number"
                  min={30}
                  max={3600}
                  value={v.search_ttl}
                  onChange={(e) => update("search_ttl", Number(e.target.value))}
                />
              </Field>
              <Field label="网页资料有效期（秒）">
                <input
                  type="number"
                  min={60}
                  max={604800}
                  value={v.page_ttl}
                  onChange={(e) => update("page_ttl", Number(e.target.value))}
                />
              </Field>
              <Field label="每日采集额度">
                <input
                  type="number"
                  min={1}
                  max={500}
                  value={v.daily_budget}
                  onChange={(e) =>
                    update("daily_budget", Number(e.target.value))
                  }
                />
              </Field>
            </div>
          </Section>
          <Section
            title="订阅来源"
            description="每条来源只供指定群使用。"
            action={
              <Button
                onClick={() =>
                  update("sources", [
                    ...v.sources,
                    {
                      group_id: groups.data?.groups?.[0]?.group_id || "",
                      url: "",
                      interval: 3600,
                      ttl: 86400,
                    },
                  ])
                }
                disabled={v.sources.length >= 32}
              >
                <Plus size={15} />
                添加来源
              </Button>
            }
          >
            {!v.sources.length ? (
              <Empty
                title="还没有订阅来源"
                description="添加公开网页，让群灵按计划更新资料。"
              />
            ) : (
              <div className="source-list">
                {v.sources.map((row: any, index: number) => (
                  <div className="source-row" key={index}>
                    <div className="form-grid">
                      <Field label="所属群">
                        <select
                          value={row.group_id}
                          onChange={(e) =>
                            update(
                              "sources",
                              v.sources.map((r: any, i: number) =>
                                i === index
                                  ? { ...r, group_id: e.target.value }
                                  : r,
                              ),
                            )
                          }
                        >
                          <option value="">选择群聊</option>
                          {groups.data?.groups?.map((g: any) => (
                            <option key={g.group_id} value={g.group_id}>
                              {g.name || g.group_id}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label="公开网页地址">
                        <input
                          type="url"
                          value={row.url}
                          placeholder="https://"
                          onChange={(e) =>
                            update(
                              "sources",
                              v.sources.map((r: any, i: number) =>
                                i === index ? { ...r, url: e.target.value } : r,
                              ),
                            )
                          }
                        />
                      </Field>
                      <Field label="刷新间隔（分钟）">
                        <input
                          type="number"
                          min={1}
                          max={10080}
                          value={row.interval / 60}
                          onChange={(e) =>
                            update(
                              "sources",
                              v.sources.map((r: any, i: number) =>
                                i === index
                                  ? {
                                      ...r,
                                      interval: Number(e.target.value) * 60,
                                    }
                                  : r,
                              ),
                            )
                          }
                        />
                      </Field>
                      <Field label="有效时长（分钟）">
                        <input
                          type="number"
                          min={1}
                          max={10080}
                          value={row.ttl / 60}
                          onChange={(e) =>
                            update(
                              "sources",
                              v.sources.map((r: any, i: number) =>
                                i === index
                                  ? { ...r, ttl: Number(e.target.value) * 60 }
                                  : r,
                              ),
                            )
                          }
                        />
                      </Field>
                    </div>
                    <Button
                      kind="danger-ghost"
                      onClick={() =>
                        update(
                          "sources",
                          v.sources.filter((_: any, i: number) => i !== index),
                        )
                      }
                    >
                      <Trash2 size={15} />
                      删除来源
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </Section>
          <Section title="采集状态">
            <div className="detail-rows">
              {Object.entries(remote.data?.status?.counters || {}).map(
                ([key, value]) => (
                  <div key={key}>
                    <span>
                      {(
                        {
                          hit: "复用命中",
                          miss: "未命中",
                          live: "实时读取",
                          collected: "采集成功",
                          failed: "采集失败",
                        } as any
                      )[key] || key}
                    </span>
                    <strong>{String(value)}</strong>
                  </div>
                ),
              )}
            </div>
            {Object.values(remote.data?.status?.sources || {}).map(
              (row: any) => (
                <div className="source-status" key={row.group_id + row.url}>
                  <Badge
                    tone={
                      row.status === "ok"
                        ? "success"
                        : row.status === "running"
                          ? "neutral"
                          : "error"
                    }
                  >
                    {row.status === "ok"
                      ? "已采集"
                      : row.status === "running"
                        ? "采集中"
                        : "失败"}
                  </Badge>
                  <span>{row.url}</span>
                  <small>上次 {time(row.checked)}</small>
                </div>
              ),
            )}
          </Section>
          <SaveBar
            dirty={draft.dirty}
            conflict={draft.conflict}
            busy={action.busy}
            error={action.error}
            onReset={draft.reset}
            onSave={() =>
              action.run(async () => {
                const next = await api("/api/knowledge", v);
                client.setQueryData(["/api/knowledge"], next);
                draft.accept(next.config);
              })
            }
          />
          {action.success && !draft.dirty && (
            <div className="saved-inline">
              <Check size={14} />
              {action.success}
            </div>
          )}
        </>
      )}
    </>
  );
}
export function System({ active }: { active: boolean }) {
  const state = useData("/api/status", 5000, active),
    version = useData("/api/update", 5000, active),
    action = useAction();
  const [tab, setTab] = useState("connection"),
    [confirm, setConfirm] = useState(false);
  const v = version.data,
    s = state.data;
  return (
    <>
      <PageTitle title="系统设置" description="连接、维护和访问管理。" />
      <div className="tabs">
        {[
          ["connection", "QQ 连接"],
          ["version", "版本与存储"],
          ["advanced", "高级参数与组件"],
          ["access", "访问管理"],
        ].map(([id, label]) => (
          <button key={id} onClick={() => setTab(id)} aria-pressed={tab === id}>
            {label}
          </button>
        ))}
      </div>
      <div className="settings-width">
        {tab === "connection" ? (
          <Section
            title="QQ 连接"
            description="登录状态与消息通道分别确认。"
            action={
              <Button onClick={() => state.refetch()}>
                <RefreshCw size={15} />
                检查状态
              </Button>
            }
          >
            {!s ? (
              <Loading />
            ) : (
              <>
                <div className="connection-summary">
                  <div className="connection-symbol">QQ</div>
                  <div>
                    <h3>
                      {s.qq_connected
                        ? "机器人在线"
                        : s.qq_logged_in
                          ? "已登录，等待通道连接"
                          : "等待 QQ 登录"}
                    </h3>
                    <p>{s.message}</p>
                  </div>
                </div>
                <div className="detail-rows">
                  <div>
                    <span>账号登录</span>
                    <Badge tone={s.qq_logged_in ? "success" : "warning"}>
                      {s.qq_logged_in ? "已登录" : "未确认"}
                    </Badge>
                  </div>
                  <div>
                    <span>消息通道</span>
                    <Badge tone={s.qq_connected ? "success" : "warning"}>
                      {s.qq_connected ? "已连接" : "未连接"}
                    </Badge>
                  </div>
                  <div>
                    <span>插件运行时</span>
                    <Badge
                      tone={s.plugin_runtime_healthy ? "success" : "warning"}
                    >
                      {s.plugin_runtime_healthy ? "正常" : "恢复中"}
                    </Badge>
                  </div>
                  <div>
                    <span>最近检查</span>
                    <span>{time(s.qq_checked_at)}</span>
                  </div>
                </div>
                {s.qr_image && (
                  <div className="qr-box">
                    <img src={s.qr_image} alt="QQ 登录二维码" />
                    <p>使用 QQ 扫码完成登录</p>
                  </div>
                )}
                {!s.qq_logged_in && (
                  <Button
                    busy={action.busy}
                    onClick={() =>
                      action.run(async () => {
                        await api("/api/qr/refresh", {});
                        await state.refetch();
                      }, "二维码已刷新")
                    }
                  >
                    <RefreshCw size={15} />
                    刷新二维码
                  </Button>
                )}
              </>
            )}
          </Section>
        ) : tab === "version" ? (
          <>
            <Section
              title="版本维护"
              description="检查已通过验收的发布版本。"
              action={
                <Badge tone={v?.busy ? "warning" : "neutral"}>
                  {v?.busy ? "更新进行中" : "空闲"}
                </Badge>
              }
            >
              <div className="version-display">
                <span>当前版本</span>
                <strong>
                  {v?.current_revision?.slice(0, 12) || "尚未读取"}
                </strong>
                <small>
                  {v?.repository || "QunLing"} · {v?.branch || "main"}
                </small>
              </div>
              <Notice tone={v?.phase === "failed" ? "error" : "neutral"}>
                {v?.message}
              </Notice>
              <div className="form-actions">
                <Button
                  disabled={v?.busy || !v?.available}
                  busy={action.busy}
                  onClick={() =>
                    action.run(async () => {
                      const result = await api("/api/update/check", {});
                      client.setQueryData(["/api/update"], result);
                      await version.refetch();
                    }, "版本检查完成")
                  }
                >
                  <RefreshCw size={15} />
                  检查更新
                </Button>
                <Button
                  kind="primary"
                  disabled={v?.busy || !v?.update_available}
                  onClick={() => setConfirm(true)}
                >
                  <Download size={15} />
                  更新到最新版本
                </Button>
              </div>
            </Section>
            <Storage active={active && tab === "version"} />
            <Section title="历史整理">
              <div className="detail-rows">
                <div>
                  <span>最近检查</span>
                  <strong>{time(v?.cleanup?.checked_at)}</strong>
                </div>
                <div>
                  <span>已整理更新记录</span>
                  <strong>{v?.cleanup?.removed_jobs ?? "—"}</strong>
                </div>
                <div>
                  <span>已清理历史镜像</span>
                  <strong>{v?.cleanup?.removed_images ?? "—"}</strong>
                </div>
              </div>
              {v?.cleanup?.status === "partial" && (
                <Notice tone="warning">
                  部分历史产物未能自动整理，需要核对归属后清理。
                </Notice>
              )}
              <p className="muted">
                历史整理保留正在使用的镜像和业务数据，清理结果以服务器报告为准。
              </p>
            </Section>
          </>
        ) : tab === "advanced" ? (
          <>
            <Advanced active={active} />
            <Components active={active} />
          </>
        ) : (
          <Section
            title="管理网址"
            description="将这个地址加入浏览器书签，下次直接打开。"
          >
            <div className="access-note">
              <h3>
                <a href={s?.management_url || location.origin}>
                  {s?.management_url || location.origin}
                </a>
              </h3>
              <p>
                服务运行在服务器上。首次登录后，同一浏览器保留登录 30 天。
                群聊、模型、记忆和 QQ 登录都在网页中管理。
              </p>
            </div>
            <div className="detail-rows">
              <div>
                <span>登录方式</span>
                <strong>管理口令 / 专属管理链接</strong>
              </div>
              <div>
                <span>浏览器会话</span>
                <strong>30 天，到期重新验证</strong>
              </div>
            </div>
            <p className="muted">使用公共电脑时，请在离开前点击右上角退出。</p>
          </Section>
        )}
        <Notice>
          {action.error ||
            (state.error && errorText(state.error)) ||
            (version.error && errorText(version.error))}
        </Notice>
        {action.success && <Notice tone="success">{action.success}</Notice>}
      </div>
      <Drawer
        open={confirm}
        onOpenChange={setConfirm}
        title="应用新版本"
        description="更新期间管理页可能短暂断开，恢复后会显示最终结果。"
      >
        <div className="detail-rows">
          <div>
            <span>当前提交</span>
            <strong>{v?.current_revision?.slice(0, 12)}</strong>
          </div>
          <div>
            <span>目标提交</span>
            <strong>{v?.latest_revision?.slice(0, 12)}</strong>
          </div>
        </div>
        <p>请先等待正在进行的长任务完成。</p>
        <Notice>{action.error}</Notice>
        <Button
          kind="primary"
          busy={action.busy}
          onClick={() =>
            action.run(async () => {
              await api("/api/update/apply", { revision: v.latest_revision });
              setConfirm(false);
              await version.refetch();
            }, "已提交更新，等待服务恢复")
          }
        >
          确认更新
        </Button>
      </Drawer>
    </>
  );
}
