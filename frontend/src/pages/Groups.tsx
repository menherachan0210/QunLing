import React, { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Check,
  ChevronRight,
  MessageSquare,
  Plus,
  RefreshCw,
  Send,
  Settings2,
  Trash2,
  X,
  Download,
  ArrowLeft,
  ArrowRight,
  Square,
} from "lucide-react";
import { api, client, errorText, refresh, time, uid, useData } from "../api";
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
const modes = {
  off: ["仅 @ 回复", "被明确叫到时回答"],
  quiet: ["安静参与", "接续与你有关的对话"],
  natural: ["自然参与", "按话题选择合适的接话时机"],
  active: ["活跃参与", "更积极参与群内讨论"],
};
const defaultGroup = () => ({
  mode: "off",
  display_name: "群灵",
  persona: "",
  examples: [],
  quiet_hours: {
    enabled: false,
    start: "23:00",
    end: "08:00",
    timezone: "Asia/Shanghai",
  },
});
function Examples({
  value,
  onChange,
}: {
  value: string[][];
  onChange: (v: string[][]) => void;
}) {
  return (
    <div className="examples">
      <div className="inline-heading">
        <div>
          <h3>对话示例</h3>
          <p>用几句真实的说话方式，示范你希望的感觉。</p>
        </div>
        <Button
          onClick={() => onChange([...value, ["", ""]])}
          disabled={value.length >= 6}
        >
          <Plus size={15} />
          添加示例
        </Button>
      </div>
      {value.map((turns, index) => (
        <div className="example-pair" key={index}>
          <div className="inline-heading">
            <span>示例 {index + 1}</span>
            <Button
              kind="ghost"
              className="icon-button"
              aria-label={"删除示例 " + (index + 1)}
              onClick={() => onChange(value.filter((_, i) => i !== index))}
            >
              <X size={15} />
            </Button>
          </div>
          {turns.map((text, i) => (
            <Field key={i} label={i % 2 ? "机器人" : "群友"}>
              <textarea
                rows={2}
                maxLength={1500}
                value={text}
                onChange={(e) =>
                  onChange(
                    value.map((row, n) =>
                      n === index
                        ? row.map((t, j) => (i === j ? e.target.value : t))
                        : row,
                    ),
                  )
                }
              />
            </Field>
          ))}
          <Button
            kind="ghost"
            onClick={() =>
              onChange(
                value.map((row, n) => (n === index ? [...row, "", ""] : row)),
              )
            }
            disabled={turns.length >= 12}
          >
            <Plus size={14} />
            再加一轮
          </Button>
        </div>
      ))}
    </div>
  );
}
function Preview({
  group,
  config,
  dirty,
  cache,
}: {
  group: string;
  config: any;
  dirty: boolean;
  cache: React.MutableRefObject<Record<string, any>>;
}) {
  const [messages, setMessages] = useState<any[]>(
      cache.current[group]?.messages || [],
    ),
    [input, setInput] = useState(cache.current[group]?.input || ""),
    [job, setJob] = useState<any>(cache.current[group]?.job || null),
    [error, setError] = useState(""),
    [details, setDetails] = useState(false),
    [member, setMember] = useState(cache.current[group]?.member || ""),
    [memory, setMemory] = useState(cache.current[group]?.memory || false),
    [starting, setStarting] = useState(false);
  useEffect(() => {
    cache.current[group] = { messages, input, job, member, memory };
  }, [messages, input, job, member, memory]);
  const bottom = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );
  const status = useQuery({
    queryKey: ["trial", group, job?.request_id],
    queryFn: () =>
      api("/api/preview", {
        action: "status",
        group_id: group,
        request_id: job.request_id,
      }),
    enabled: !!job && !starting && job.status === "running",
    refetchInterval: 1500,
    retry: false,
  });
  useEffect(() => {
    if (
      !status.data ||
      status.data.status === "running" ||
      job?.status !== "running"
    )
      return;
    const value = status.data;
    setJob(value);
    if (value.status === "completed") {
      setMessages((rows) => [
        ...rows,
        {
          role: "assistant",
          text: value.reply,
          notice:
            value.outcome === "silence"
              ? "这轮判断为保持安静。"
              : value.outcome === "wait"
                ? "这轮判断为等待后续消息。"
                : value.outcome === "requires_tools"
                  ? "这条消息需要工具处理；试聊中未执行工具。"
                  : "",
        },
      ]);
    } else if (value.status === "failed") setError(value.error || "试聊未完成");
  }, [status.data]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [messages, starting, job?.status]);
  const busy = starting || job?.status === "running";
  const send = async () => {
    if (!input.trim() || busy) return;
    if (memory && !/^[1-9][0-9]{0,19}$/.test(member)) {
      setError("请填写有效的本群成员 QQ 号。");
      return;
    }
    const rows = [
      ...messages
        .filter((row) => row.text)
        .map(({ role, text }) => ({ role, text })),
      { role: "user", text: input.trim() },
    ].slice(-24);
    while (rows[0]?.role === "assistant") rows.shift();
    setMessages(rows);
    setInput("");
    setError("");
    setStarting(true);
    const requestId = uid();
    const pending = {
      request_id: requestId,
      group_id: group,
      status: "running",
    };
    setJob(pending);
    try {
      const result = await api("/api/preview", {
        action: "start",
        group_id: group,
        request_id: requestId,
        config,
        messages: rows,
        member_id: member,
        use_memory: memory,
      });
      cache.current[group] = {
        messages: rows,
        input: "",
        job: result,
        member,
        memory,
      };
      if (mounted.current) setJob(result);
    } catch (e) {
      cache.current[group] = {
        messages: rows.slice(0, -1),
        input: rows.at(-1)?.text || "",
        job: null,
        member,
        memory,
      };
      if (mounted.current) {
        setJob(null);
        setError(errorText(e));
        setInput(rows.at(-1)?.text || "");
        setMessages(rows.slice(0, -1));
      }
    } finally {
      if (mounted.current) setStarting(false);
    }
  };
  return (
    <aside className="trial-panel">
      <div className="trial-heading">
        <div>
          <MessageSquare size={18} />
          <strong>试聊</strong>
          <Badge tone={dirty ? "warning" : "neutral"}>
            {dirty ? "当前草稿" : "已保存设置"}
          </Badge>
        </div>
        <Button
          kind="ghost"
          className="icon-button"
          aria-label="清空试聊"
          disabled={busy}
          onClick={() => {
            setMessages([]);
            setJob(null);
            setError("");
          }}
        >
          <RefreshCw size={16} />
        </Button>
      </div>
      <div className="trial-context">
        <span>模拟直接对话 · 使用本群人格与模型</span>
        <small>仅在此预览 · 不发到 QQ · 不写入记忆</small>
      </div>
      <details className="trial-options">
        <summary>试聊记忆设置</summary>
        <Toggle
          checked={memory}
          onChange={setMemory}
          label="读取本群成员记忆"
          disabled={busy}
        />
        {memory && (
          <Field label="成员 QQ 号">
            <input
              inputMode="numeric"
              value={member}
              onChange={(e) => setMember(e.target.value)}
              disabled={busy}
            />
          </Field>
        )}
      </details>
      <div className="trial-messages">
        {!messages.length && (
          <div className="trial-intro">
            <span className="trial-orbit">
              <MessageSquare size={26} />
            </span>
            <h3>听听它会怎么说</h3>
            <p>
              先写一句话，看看当前人格的回答。
              <br />
              调整左侧设置后，可以继续比较。
            </p>
            <div className="suggestions">
              {["今天终于把事情忙完了", "怎么感觉你又开始讲大道理了"].map(
                (t) => (
                  <button key={t} onClick={() => setInput(t)}>
                    {t}
                    <ArrowRight size={14} />
                  </button>
                ),
              )}
            </div>
          </div>
        )}
        {messages.map((row, i) => (
          <div className={`trial-message ${row.role}`} key={i}>
            <small>
              {row.role === "user"
                ? "你"
                : config.groups[group]?.display_name || "群灵"}
            </small>
            <div>{row.text || <em>{row.notice}</em>}</div>
          </div>
        ))}
        {busy && (
          <div className="trial-thinking">
            <i />
            <i />
            <i />
            <span>正在理解和回复</span>
          </div>
        )}
        <div ref={bottom} />
      </div>
      {job?.status === "completed" && (
        <button className="trial-result" onClick={() => setDetails(true)}>
          查看本轮使用的人格、记忆与耗时
          <ChevronRight size={14} />
        </button>
      )}
      <Notice>{error || (status.error && errorText(status.error))}</Notice>
      <form
        className="trial-composer"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <textarea
          rows={3}
          aria-label="试聊消息"
          placeholder="输入一句话…"
          maxLength={3000}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div>
          <small>Enter 发送 · Shift Enter 换行</small>
          {busy ? (
            <Button
              aria-label="取消试聊"
              onClick={async () => {
                if (job) {
                  try {
                    const v = await api("/api/preview", {
                      action: "cancel",
                      group_id: group,
                      request_id: job.request_id,
                    });
                    setJob(v);
                    await client.invalidateQueries({
                      queryKey: ["trial", group],
                    });
                  } catch (e) {
                    setError(errorText(e));
                  }
                }
              }}
              disabled={starting}
            >
              <Square size={14} />
            </Button>
          ) : (
            <Button
              type="submit"
              kind="primary"
              disabled={!input.trim()}
              aria-label="发送试聊"
            >
              <Send size={16} />
            </Button>
          )}
        </div>
      </form>
      <small className="trial-cost">
        发送试聊会调用已配置的模型，按服务商规则计费。
      </small>
      <Drawer
        open={details}
        onOpenChange={setDetails}
        title="本轮试聊详情"
        description="这里只展示实际使用的输入与运行结果。"
      >
        <Section title="各阶段耗时">
          <div className="detail-rows">
            {job?.stages?.map((s: any, i: number) => (
              <div key={i}>
                <span>
                  {(
                    {
                      judge: "理解与参与判断",
                      generate: "生成回复",
                      review: "内容复核",
                    } as any
                  )[s.stage] || s.stage}
                </span>
                <strong>{s.seconds} 秒</strong>
              </div>
            ))}
          </div>
          <p className="muted">
            {job?.review_changed
              ? "内容复核调整了回复。"
              : "最终保留了生成的回复，或本轮选择不回复。"}
          </p>
        </Section>
        <Section title="使用的人格">
          <pre className="readable-pre">{job?.persona}</pre>
        </Section>
        <Section title="使用的记忆">
          <pre className="readable-pre">
            {JSON.stringify(job?.memory || {}, null, 2)}
          </pre>
        </Section>
      </Drawer>
    </aside>
  );
}
function Memory({ group, active }: { group: string; active: boolean }) {
  const [cached] = useState(() => {
    try {
      return JSON.parse(
        sessionStorage.getItem("ql:draft:memory:" + group) || "null",
      );
    } catch {
      return null;
    }
  });
  const [query, setQuery] = useState(""),
    [search, setSearch] = useState(""),
    [kind, setKind] = useState("all"),
    [offset, setOffset] = useState(0),
    [item, setItem] = useState<any>(cached?.item || null),
    [value, setValue] = useState<any>(cached?.value || null),
    [remove, setRemove] = useState(false);
  const [discard, setDiscard] = useState(false);
  const dirty = !!item && JSON.stringify(value) !== JSON.stringify(item.value);
  useEffect(() => {
    const key = "ql:draft:memory:" + group;
    if (dirty) sessionStorage.setItem(key, JSON.stringify({ item, value }));
    else sessionStorage.removeItem(key);
  }, [group, item, value, dirty]);
  useEffect(() => {
    if (!dirty) return;
    const prevent = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty]);
  const action = useAction();
  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(query);
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [query]);
  const result = useQuery({
    queryKey: ["memory", group, search, kind, offset],
    queryFn: () =>
      api("/api/memory", {
        action: "list",
        group_id: group,
        query: search,
        kind,
        offset,
      }),
    enabled: active,
  });
  const mutate = async (mode: string) => {
    if (!item) return;
    const body: any = {
      action: mode,
      group_id: group,
      kind: item.kind,
      revision: item.revision,
    };
    if (item.kind === "episode") {
      body.id = item.id;
      body.collection_id = item.collection_id;
    } else body.sender_id = item.sender_id;
    if (mode === "save") body.value = value;
    await action.run(async () => {
      const reply = await api("/api/memory", body);
      if (
        reply.group_id !== group ||
        !reply[mode === "save" ? "saved" : "deleted"]
      )
        throw Error("操作结果未确认，请刷新核对。");
      setItem(null);
      setRemove(false);
      await client.invalidateQueries({ queryKey: ["memory", group] });
    });
  };
  return (
    <Section
      title="本群记忆"
      description="成员资料、互动约定和长期事件，按群隔离。"
      action={
        <Button
          busy={action.busy}
          onClick={() =>
            action.run(async () => {
              const data = await api("/api/memory", {
                action: "export",
                group_id: group,
              });
              const url = URL.createObjectURL(
                new Blob([JSON.stringify(data, null, 2)], {
                  type: "application/json",
                }),
              );
              const a = document.createElement("a");
              a.href = url;
              a.download = `群灵记忆-${group}.json`;
              a.click();
              setTimeout(() => URL.revokeObjectURL(url), 2000);
            }, "已导出")
          }
        >
          <Download size={15} />
          导出
        </Button>
      }
    >
      <div className="filter-row">
        <SearchField
          value={query}
          onChange={setQuery}
          placeholder="搜索记忆内容或成员"
        />
        <select
          aria-label="记忆类型"
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setOffset(0);
          }}
        >
          <option value="all">全部类型</option>
          <option value="profile">成员资料</option>
          <option value="interaction">互动约定</option>
          <option value="episode">长期事件</option>
        </select>
        <Button onClick={() => result.refetch()} aria-label="刷新记忆">
          <RefreshCw size={15} />
        </Button>
      </div>
      {result.isPending ? (
        <Loading />
      ) : result.error ? (
        <Notice>{errorText(result.error)}</Notice>
      ) : !result.data?.items?.length ? (
        <Empty
          title="没有匹配的记忆"
          description="试着更换关键词，或等群聊产生新的记忆。"
        />
      ) : (
        <div className="memory-list">
          {result.data.items.map((row: any, i: number) => (
            <button
              key={row.id || row.kind + row.sender_id || i}
              className="memory-row"
              onClick={() => {
                setItem(row);
                setValue(structuredClone(row.value));
                setRemove(false);
                action.setError("");
              }}
            >
              <div>
                <Badge>
                  {
                    (
                      {
                        profile: "成员资料",
                        interaction: "互动约定",
                        episode: "长期事件",
                      } as any
                    )[row.kind]
                  }
                </Badge>
                <time>{time(row.updated_at)}</time>
              </div>
              <strong>{row.title}</strong>
              <p>
                {typeof row.value === "string"
                  ? row.value
                  : Object.values(row.value).flat().filter(Boolean).join(" · ")}
              </p>
            </button>
          ))}
        </div>
      )}
      <div className="pagination">
        <small>{result.data?.total ?? 0} 条记忆</small>
        <Button
          disabled={!offset || result.isFetching}
          onClick={() => setOffset(Math.max(0, offset - 30))}
        >
          <ArrowLeft size={14} />
          上一页
        </Button>
        <Button
          disabled={result.data?.next_offset == null || result.isFetching}
          onClick={() => setOffset(result.data.next_offset)}
        >
          下一页
          <ArrowRight size={14} />
        </Button>
      </div>
      <Notice>{action.error}</Notice>
      <Drawer
        open={!!item}
        onOpenChange={(open) => {
          if (!open && !action.busy) {
            if (dirty) setDiscard(true);
            else setItem(null);
          }
        }}
        title={item?.title || "编辑记忆"}
        description="保存时检查版本，避免覆盖同时发生的更新。"
      >
        {item && (
          <>
            {discard && (
              <Notice tone="warning">
                这条记忆还有未保存的修改。
                <div className="form-actions">
                  <Button onClick={() => setDiscard(false)}>继续编辑</Button>
                  <Button
                    kind="danger"
                    onClick={() => {
                      setItem(null);
                      setDiscard(false);
                    }}
                  >
                    放弃并关闭
                  </Button>
                </div>
              </Notice>
            )}
            <div className="form-stack">
              {item.kind === "episode" ? (
                <Field label="记忆内容">
                  <textarea
                    rows={12}
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                  />
                </Field>
              ) : item.kind === "interaction" ? (
                <>
                  <Field label="接话方式">
                    <select
                      value={value.mode}
                      onChange={(e) =>
                        setValue({ ...value, mode: e.target.value })
                      }
                    >
                      <option value="normal">正常参与</option>
                      <option value="mention_only">仅真正 @ 时回复</option>
                      <option value="conditional">按条件参与</option>
                    </select>
                  </Field>
                  <Field label="接话条件">
                    <textarea
                      rows={5}
                      value={value.rule}
                      onChange={(e) =>
                        setValue({ ...value, rule: e.target.value })
                      }
                    />
                  </Field>
                </>
              ) : (
                ["name", "traits", "preferences", "notes"].map((key) => (
                  <Field
                    key={key}
                    label={
                      (
                        {
                          name: "称呼",
                          traits: "特点（每行一项）",
                          preferences: "偏好（每行一项）",
                          notes: "补充",
                        } as any
                      )[key]
                    }
                  >
                    <textarea
                      rows={key === "name" ? 1 : 4}
                      value={
                        Array.isArray(value[key])
                          ? value[key].join("\n")
                          : value[key] || ""
                      }
                      onChange={(e) =>
                        setValue({
                          ...value,
                          [key]: ["traits", "preferences"].includes(key)
                            ? e.target.value.split("\n").filter(Boolean)
                            : e.target.value,
                        })
                      }
                    />
                  </Field>
                ))
              )}
            </div>
            <Notice>{action.error}</Notice>
            <div className="drawer-footer">
              <Button
                kind="danger-ghost"
                disabled={action.busy}
                onClick={() => setRemove(!remove)}
              >
                <Trash2 size={15} />
                删除
              </Button>
              <Button
                kind="primary"
                busy={action.busy}
                onClick={() => mutate("save")}
              >
                保存修改
              </Button>
            </div>
            {remove && (
              <Notice tone="warning">
                删除后无法恢复。
                <Button
                  kind="danger"
                  busy={action.busy}
                  onClick={() => mutate("delete")}
                >
                  确认删除这条记忆
                </Button>
              </Notice>
            )}
          </>
        )}
      </Drawer>
    </Section>
  );
}
export function Groups({ active }: { active: boolean }) {
  const config = useData("/api/proactive", 8000, active),
    list = useData("/api/proactive/groups", 30000, active),
    draft = useDraft("groups", config.data?.config),
    action = useAction();
  const trialCache = useRef<Record<string, any>>({});
  const [selected, setSelected] = useState(
      () => sessionStorage.getItem("ql:group") || "",
    ),
    [search, setSearch] = useState(""),
    [tab, setTab] = useState("persona"),
    [defaults, setDefaults] = useState(false),
    [reset, setReset] = useState(false),
    [confirmation, setConfirmation] = useState("");
  useEffect(() => {
    const select = () => {
      setSelected(sessionStorage.getItem("ql:group") || "");
      setDefaults(false);
    };
    window.addEventListener("ql-group-selected", select);
    return () => window.removeEventListener("ql-group-selected", select);
  }, []);
  const groups: any[] =
    list.data?.groups ||
    Object.keys(draft.value?.groups || {}).map((group_id) => ({
      group_id,
      name: group_id,
    }));
  const group = groups.find((g) => g.group_id === selected) || groups[0];
  const gid = group?.group_id || "";
  const value = draft.value;
  const policy = value?.groups?.[gid] || defaultGroup();
  const update = (patch: any) =>
    draft.setValue({
      ...value,
      groups: { ...value.groups, [gid]: { ...policy, ...patch } },
    });
  const choose = (id: string) => {
    setSelected(id);
    sessionStorage.setItem("ql:group", id);
    setDefaults(false);
  };
  const save = () =>
    action.run(async () => {
      const next = await api("/api/proactive", value);
      if (!next.config) throw Error("未收到保存确认。");
      client.setQueryData(["/api/proactive"], next);
      draft.accept(next.config);
      await refresh("/api/proactive");
    });
  return (
    <>
      <PageTitle title="群聊与人格" description="让每个群都有合适的交流方式。">
        <Button onClick={() => setDefaults(!defaults)}>
          <Settings2 size={15} />
          默认设置
        </Button>
        <Button
          onClick={() => {
            config.refetch();
            list.refetch();
          }}
          aria-label="刷新群聊"
        >
          <RefreshCw size={15} />
        </Button>
      </PageTitle>
      {config.error && <Notice>{errorText(config.error)}</Notice>}
      {list.error && (
        <Notice tone="warning">
          {errorText(list.error)} 已保存的设置仍可查看。
        </Notice>
      )}
      {!value ? (
        <Loading />
      ) : (
        <>
          <div className="group-workspace">
            <aside className="group-sidebar">
              <SearchField
                value={search}
                onChange={setSearch}
                placeholder="搜索群名或群号"
              />
              <div className="group-list-caption">
                群聊 <span>{groups.length}</span>
              </div>
              <div className="group-list">
                {groups
                  .filter((g) => (g.name + g.group_id).includes(search))
                  .map((g, i) => (
                    <button
                      key={g.group_id}
                      className={
                        gid === g.group_id && !defaults ? "selected" : ""
                      }
                      onClick={() => choose(g.group_id)}
                    >
                      <span className={`avatar color-${i % 4}`}>
                        {g.name?.slice(0, 1) || "群"}
                      </span>
                      <span>
                        <strong>{g.name || g.group_id}</strong>
                        <small>{g.group_id}</small>
                      </span>
                      <i
                        className={`event-dot ${value.enabled && value.groups[g.group_id]?.mode && value.groups[g.group_id]?.mode !== "off" ? "success" : "neutral"}`}
                      />
                    </button>
                  ))}
              </div>
              <div className="group-global">
                <Toggle
                  checked={value.enabled}
                  onChange={(enabled) => draft.setValue({ ...value, enabled })}
                  label="自由参与"
                  description="所有群的总开关"
                />
              </div>
            </aside>
            {defaults ? (
              <div className="group-main">
                <Section
                  title="默认人格"
                  description="未单独设置人格的群使用这里的内容。"
                >
                  <Field label="性格与交流习惯">
                    <textarea
                      rows={13}
                      maxLength={6000}
                      placeholder={
                        config.data?.builtin_style || "描述你希望的交流方式"
                      }
                      value={value.default_style || ""}
                      onChange={(e) =>
                        draft.setValue({
                          ...value,
                          default_style: e.target.value,
                        })
                      }
                    />
                  </Field>
                  <Examples
                    value={value.default_examples || []}
                    onChange={(default_examples) =>
                      draft.setValue({ ...value, default_examples })
                    }
                  />
                </Section>
                <Section title="QQ 工具">
                  <Toggle
                    label="允许使用 QQ 工具"
                    description="具体群权限仍单独控制。"
                    checked={value.qq_actions?.enabled || false}
                    onChange={(enabled) =>
                      draft.setValue({
                        ...value,
                        qq_actions: {
                          owner_ids: [],
                          transcription_model: "",
                          ...value.qq_actions,
                          enabled,
                        },
                      })
                    }
                  />
                  <Field
                    label="机器人管理员 QQ 号"
                    hint="每行一个，最多 20 个。"
                  >
                    <textarea
                      rows={3}
                      value={(value.qq_actions?.owner_ids || []).join("\n")}
                      onChange={(e) =>
                        draft.setValue({
                          ...value,
                          qq_actions: {
                            enabled: false,
                            transcription_model: "",
                            ...value.qq_actions,
                            owner_ids: e.target.value
                              .split("\n")
                              .filter(Boolean),
                          },
                        })
                      }
                    />
                  </Field>
                </Section>
              </div>
            ) : !gid ? (
              <div className="group-main">
                <Empty
                  title="先连接 QQ"
                  description="连接后选择一个群，设置人格并开始试聊。"
                />
              </div>
            ) : (
              <>
                <div className="group-main">
                  <div className="group-heading">
                    <div>
                      <h2>{group.name || gid}</h2>
                      <span>{gid}</span>
                    </div>
                    <Badge
                      tone={
                        policy.persona || policy.examples?.length
                          ? "success"
                          : "neutral"
                      }
                    >
                      {policy.persona || policy.examples?.length
                        ? "独立人格"
                        : "继承默认"}
                    </Badge>
                  </div>
                  <div className="tabs" role="tablist">
                    {[
                      ["persona", "人格"],
                      ["participation", "参与方式"],
                      ["memory", "记忆"],
                      ["runtime", "运行情况"],
                    ].map(([id, label]) => (
                      <button
                        role="tab"
                        aria-selected={tab === id}
                        key={id}
                        onClick={() => setTab(id)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  {tab === "persona" ? (
                    <div className="form-stack">
                      <Field label="在这个群里的称呼">
                        <input
                          value={policy.display_name}
                          maxLength={40}
                          onChange={(e) =>
                            update({ display_name: e.target.value })
                          }
                        />
                      </Field>
                      <Field
                        label="性格与交流习惯"
                        hint="留空继承默认人格；填写后，这个群使用自己的设定。"
                      >
                        <textarea
                          className="persona-input"
                          rows={11}
                          maxLength={6000}
                          value={policy.persona}
                          placeholder="它是什么样的人？平时怎么说话，怎么和群友相处？"
                          onChange={(e) => update({ persona: e.target.value })}
                        />
                      </Field>
                      <div className="field-foot">
                        <span>适用于 @ 回复、自由接话与工具完成后的回复</span>
                        <span>{policy.persona.length} / 6000</span>
                      </div>
                      <Examples
                        value={policy.examples || []}
                        onChange={(examples) => update({ examples })}
                      />
                    </div>
                  ) : tab === "participation" ? (
                    <>
                      <Section title="参与方式">
                        <div className="mode-options">
                          {Object.entries(modes).map(
                            ([mode, [label, note]]) => (
                              <button
                                key={mode}
                                className={
                                  policy.mode === mode ? "selected" : ""
                                }
                                onClick={() => update({ mode })}
                              >
                                <span>
                                  <strong>{label}</strong>
                                  <small>{note}</small>
                                </span>
                                <span className="radio">
                                  {policy.mode === mode && <Check size={12} />}
                                </span>
                              </button>
                            ),
                          )}
                        </div>
                      </Section>
                      <Section title="安静时段">
                        <Toggle
                          label="在指定时段保持安静"
                          checked={policy.quiet_hours.enabled}
                          onChange={(enabled) =>
                            update({
                              quiet_hours: { ...policy.quiet_hours, enabled },
                            })
                          }
                        />
                        {policy.quiet_hours.enabled && (
                          <div className="form-grid">
                            <Field label="开始">
                              <input
                                type="time"
                                value={policy.quiet_hours.start}
                                onChange={(e) =>
                                  update({
                                    quiet_hours: {
                                      ...policy.quiet_hours,
                                      start: e.target.value,
                                    },
                                  })
                                }
                              />
                            </Field>
                            <Field label="结束">
                              <input
                                type="time"
                                value={policy.quiet_hours.end}
                                onChange={(e) =>
                                  update({
                                    quiet_hours: {
                                      ...policy.quiet_hours,
                                      end: e.target.value,
                                    },
                                  })
                                }
                              />
                            </Field>
                            <Field label="时区">
                              <input
                                value={policy.quiet_hours.timezone}
                                onChange={(e) =>
                                  update({
                                    quiet_hours: {
                                      ...policy.quiet_hours,
                                      timezone: e.target.value,
                                    },
                                  })
                                }
                              />
                            </Field>
                          </div>
                        )}
                      </Section>
                      <Section title="本群 QQ 工具权限">
                        {Object.entries({
                          interaction: "轻量互动",
                          management: "群管理",
                          files: "文件操作",
                          media: "媒体操作",
                        }).map(([key, label]) => (
                          <Toggle
                            key={key}
                            label={label}
                            checked={
                              policy.qq_permissions?.[key] ??
                              key === "interaction"
                            }
                            onChange={(v) =>
                              update({
                                qq_permissions: {
                                  interaction: true,
                                  management: false,
                                  files: false,
                                  media: false,
                                  ...policy.qq_permissions,
                                  [key]: v,
                                },
                              })
                            }
                          />
                        ))}
                      </Section>
                    </>
                  ) : tab === "memory" ? (
                    <Memory key={gid} group={gid} active={active} />
                  ) : (
                    <Section
                      title="运行情况"
                      description="以下按服务器已保存的设置运行。"
                    >
                      <div className="detail-rows">
                        <div>
                          <span>最近发送</span>
                          <strong>
                            {time(
                              config.data?.status?.groups?.[gid]?.last_sent_at,
                            )}
                          </strong>
                        </div>
                        <div>
                          <span>最近一小时判断</span>
                          <strong>
                            {config.data?.status?.groups?.[gid]
                              ?.judgments_hour ?? "—"}
                          </strong>
                        </div>
                        <div>
                          <span>最近一小时回复</span>
                          <strong>
                            {config.data?.status?.groups?.[gid]?.replies_hour ??
                              "—"}
                          </strong>
                        </div>
                        {Object.entries(
                          config.data?.status?.groups?.[gid]?.performance
                            ?.stages || {},
                        ).map(([key, stats]: any) => (
                          <div key={key}>
                            <span>
                              {(
                                {
                                  judge: "参与判断",
                                  generate: "生成",
                                  review: "内容复核",
                                  review_gate: "Jev 快速复核",
                                  reconcile: "去重",
                                } as any
                              )[key] || key}
                            </span>
                            <strong>
                              中位 {stats.p50_seconds} 秒 · {stats.count} 次
                            </strong>
                          </div>
                        ))}
                      </div>
                      <Button
                        kind="danger-ghost"
                        onClick={() => {
                          setReset(true);
                          setConfirmation("");
                        }}
                      >
                        清空本群全部记忆
                      </Button>
                    </Section>
                  )}
                </div>
                {tab === "persona" && (
                  <Preview
                    key={gid}
                    group={gid}
                    config={{
                      ...value,
                      groups: { ...value.groups, [gid]: policy },
                    }}
                    dirty={draft.dirty}
                    cache={trialCache}
                  />
                )}
              </>
            )}
          </div>
          <SaveBar
            dirty={draft.dirty}
            busy={action.busy}
            conflict={draft.conflict}
            error={action.error}
            onSave={save}
            onReset={draft.reset}
          />
          {action.success && !draft.dirty && (
            <div className="saved-inline">
              <Check size={14} />
              {action.success}
            </div>
          )}
        </>
      )}
      <Drawer
        open={reset}
        onOpenChange={(v) => !action.busy && setReset(v)}
        title="清空本群全部记忆"
        description="将移除本群的长期记忆、画像与互动约定，此操作不可撤销。"
      >
        <Field label={`输入群号 ${gid} 确认`}>
          <input
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
          />
        </Field>
        <Notice>{action.error}</Notice>
        <Button
          kind="danger"
          disabled={confirmation !== gid}
          busy={action.busy}
          onClick={() =>
            action.run(async () => {
              const receipt = await api("/api/proactive/memory/reset", {
                group_id: gid,
                confirmation,
                request_id: uid(),
              });
              if (!receipt.cleared || receipt.group_id !== gid)
                throw Error("清空结果未确认，请刷新核对。");
              setReset(false);
              await client.invalidateQueries({ queryKey: ["memory", gid] });
            }, "本群记忆已清空")
          }
        >
          清空记忆
        </Button>
      </Drawer>
    </>
  );
}
