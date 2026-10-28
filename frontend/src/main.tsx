import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import {
  Activity,
  MessagesSquare,
  SlidersHorizontal,
  ListChecks,
  Library,
  Settings2,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  LogOut,
  ArrowUpRight,
  RefreshCw,
  ArrowRight,
  Command,
  MessageCircle,
} from "lucide-react";
import { api, client, useData, time, errorText } from "./api";
import {
  Badge,
  Button,
  Drawer,
  Empty,
  Field,
  Loading,
  Notice,
  PageTitle,
  SearchField,
  Section,
} from "./ui";
import { Groups } from "./pages/Groups";
import { Models } from "./pages/Models";
import { Records, Knowledge, System } from "./pages/Operations";
import "./style.css";
const pages = [
  ["overview", "总览", Activity],
  ["groups", "群聊与人格", MessagesSquare],
  ["models", "模型与能力", SlidersHorizontal],
  ["records", "任务与记录", ListChecks],
  ["knowledge", "资料与知识", Library],
  ["system", "系统设置", Settings2],
] as const;
const route = () => {
  const p = location.hash.slice(1).split("/")[0];
  return pages.some((x) => x[0] === p) ? p : "overview";
};
function Login({ onLogin }: { onLogin: () => void }) {
  const [token, setToken] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const login = async (value: string) => {
    setBusy(true);
    setError("");
    try {
      await api("/api/session", { token: value });
      sessionStorage.removeItem("qqbot-setup");
      onLogin();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    const params = new URLSearchParams(location.hash.slice(1));
    const value = params.get("token");
    if (value) {
      history.replaceState(null, "", location.pathname);
      login(value);
    }
  }, []);
  return (
    <main className="login-page">
      <div className="login-identity">
        <span className="mark">群</span>
        <span>
          群灵<span className="dim"> / 管理</span>
        </span>
      </div>
      <form
        className="login-form"
        onSubmit={(e) => {
          e.preventDefault();
          login(token);
        }}
      >
        <span className="eyebrow">欢迎回来</span>
        <h1>打开你的群灵</h1>
        <p>连接这台服务器，继续管理群聊与机器人。</p>
        <Field label="管理口令" hint="使用你的专属管理链接也可以直接登录。">
          <input
            type="password"
            autoComplete="current-password"
            required
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        </Field>
        <Notice>{error}</Notice>
        <Button type="submit" kind="primary" busy={busy}>
          登录管理台
          <ArrowRight size={16} />
        </Button>
        <small>仅在这台浏览器保留登录，30 天后重新验证。</small>
      </form>
      <div className="login-foot">QunLing · 让群聊回到对话本身</div>
    </main>
  );
}
function Overview({
  navigate,
  active,
}: {
  navigate: (id: string) => void;
  active: boolean;
}) {
  const state = useData("/api/status", 5000, active),
    groups = useData("/api/proactive/groups", 30000, active),
    runtime = useData("/api/proactive", 8000, active),
    requests = useData("/api/requests?limit=6", 10000, active);
  const data = state.data;
  const rows = groups.data?.groups || [];
  const stats = runtime.data?.status?.groups || {};
  return (
    <>
      <PageTitle
        title="总览"
        description="连接、群聊和最近发生的事，都在这里。"
      >
        <Button onClick={() => client.invalidateQueries()}>
          <RefreshCw size={15} />
          刷新
        </Button>
      </PageTitle>
      {state.error && <Notice>{errorText(state.error)}</Notice>}
      <div className="overview-hero">
        <div className="hero-symbol">
          <MessageCircle size={30} />
        </div>
        <div>
          <span className="eyebrow">当前状态</span>
          <h2>
            {!data
              ? "正在连接管理服务"
              : data.qq_connected && data.plugins_ready
                ? "群灵已就绪"
                : data.message || "机器人正在准备"}
          </h2>
          <p>
            {data?.qq_connected
              ? "QQ 消息通道已连接，你可以继续管理群聊。"
              : "前往系统设置检查 QQ 登录与功能组件。"}
          </p>
        </div>
        <Button
          onClick={() => navigate(data?.qq_connected ? "groups" : "system")}
        >
          {data?.qq_connected ? "管理群聊" : "检查连接"}
          <ArrowUpRight size={16} />
        </Button>
      </div>
      <div className="metrics">
        <div>
          <span>QQ 连接</span>
          <strong>
            {data ? (data.qq_connected ? "在线" : "待连接") : "—"}
          </strong>
        </div>
        <div>
          <span>已加入的群</span>
          <strong>
            {groups.data ? rows.length : "—"}
            <small>个</small>
          </strong>
        </div>
        <div>
          <span>参与对话</span>
          <strong>
            {runtime.data
              ? Object.values(runtime.data.config.groups || {}).filter(
                  (v: any) => v.mode !== "off",
                ).length
              : "—"}
            <small>个群</small>
          </strong>
        </div>
        <div>
          <span>功能组件</span>
          <strong>
            {data ? (data.plugin_runtime_healthy ? "正常" : "检查中") : "—"}
          </strong>
        </div>
      </div>
      <div className="overview-columns">
        <Section
          title="你的群聊"
          description="人格、参与方式与成员记忆"
          action={
            <Button onClick={() => navigate("groups")}>
              查看全部
              <ArrowRight size={14} />
            </Button>
          }
        >
          {groups.isPending ? (
            <Loading />
          ) : groups.error ? (
            <Notice>{errorText(groups.error)}</Notice>
          ) : !rows.length ? (
            <Empty
              title="暂无可读取的群"
              description="QQ 登录后，群列表会自动出现。"
            />
          ) : (
            <div className="list">
              {rows.slice(0, 6).map((g: any, i: number) => (
                <button
                  className="group-overview-row"
                  key={g.group_id}
                  onClick={() => {
                    sessionStorage.setItem("ql:group", g.group_id);
                    window.dispatchEvent(new Event("ql-group-selected"));
                    navigate("groups");
                  }}
                >
                  <span className={`avatar color-${i % 4}`}>
                    {g.name?.slice(0, 1) || "群"}
                  </span>
                  <span className="grow">
                    <strong>{g.name || g.group_id}</strong>
                    <small>{g.group_id}</small>
                  </span>
                  <Badge
                    tone={
                      runtime.data?.config?.groups[g.group_id]?.mode &&
                      runtime.data.config.groups[g.group_id].mode !== "off"
                        ? "success"
                        : "neutral"
                    }
                  >
                    {runtime.data?.config?.groups[g.group_id]?.mode === "off"
                      ? "仅 @ 回复"
                      : runtime.data?.config?.groups[g.group_id]?.mode
                        ? "参与中"
                        : "未设置"}
                  </Badge>
                  <ArrowUpRight size={15} />
                </button>
              ))}
            </div>
          )}
        </Section>
        <Section
          title="最近请求"
          description="真实处理记录，点击查看经过"
          action={
            <Button onClick={() => navigate("records")}>
              全部记录
              <ArrowRight size={14} />
            </Button>
          }
        >
          {requests.isPending ? (
            <Loading />
          ) : requests.error ? (
            <Notice>{errorText(requests.error)}</Notice>
          ) : !requests.data?.requests?.length ? (
            <Empty
              title="暂时没有请求记录"
              description="收到群消息后，在这里查看处理结果。"
            />
          ) : (
            <div className="list">
              {requests.data.requests.map((r: any) => (
                <button
                  className="activity-row"
                  key={r.request_id}
                  onClick={() => navigate("records")}
                >
                  <span
                    className={`event-dot ${r.status === "failed" ? "error" : "success"}`}
                  />
                  <span className="grow">
                    <strong>
                      {r.status === "failed"
                        ? "请求未完成"
                        : r.status === "completed"
                          ? "回复已发送"
                          : "消息处理记录"}
                    </strong>
                    <small>
                      {r.group_id
                        ? "群 " + r.group_id
                        : r.launcher_id || r.request_id.slice(0, 10)}
                    </small>
                  </span>
                  <time>{time(r.created || r.created_at || r.started_at)}</time>
                </button>
              ))}
            </div>
          )}
        </Section>
      </div>
    </>
  );
}
function Console() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null),
    [page, setPage] = useState(route),
    [collapsed, setCollapsed] = useState(false),
    [searchOpen, setSearchOpen] = useState(false),
    [query, setQuery] = useState(""),
    [logoutError, setLogoutError] = useState("");
  const state = useData("/api/status", 5000, authenticated === true);
  const navigate = (id: string) => {
    location.hash = id;
    setSearchOpen(false);
  };
  useEffect(() => {
    const authenticate = (initial = false) => {
      const bootstrap = new URLSearchParams(location.hash.slice(1)).get(
        "token",
      );
      if (!initial && !bootstrap) return;
      if (bootstrap) history.replaceState(null, "", location.pathname);
      (bootstrap
        ? api("/api/session", { token: bootstrap })
        : api("/api/session")
      )
        .then((v) => setAuthenticated(v.authenticated))
        .catch(() => setAuthenticated(false));
    };
    authenticate(true);
    const onHash = () => authenticate();
    window.addEventListener("hashchange", onHash);
    const lock = () => {
      setAuthenticated(false);
      client.clear();
    };
    window.addEventListener("console-locked", lock);
    return () => {
      window.removeEventListener("console-locked", lock);
      window.removeEventListener("hashchange", onHash);
    };
  }, []);
  useEffect(() => {
    const fn = () => setPage(route());
    window.addEventListener("hashchange", fn);
    const keyboard = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => {
      window.removeEventListener("hashchange", fn);
      window.removeEventListener("keydown", keyboard);
    };
  }, []);
  useEffect(() => {
    document.title = `${pages.find((x) => x[0] === page)?.[1]} · 群灵`;
  }, [page]);
  const logout = async () => {
    setLogoutError("");
    try {
      await api("/api/session/logout", {});
      for (const key of Object.keys(sessionStorage))
        if (key.startsWith("ql:draft:")) sessionStorage.removeItem(key);
      client.clear();
      setAuthenticated(false);
    } catch (e) {
      setLogoutError("退出未完成：" + errorText(e));
    }
  };
  if (authenticated === null)
    return (
      <div className="startup">
        <span className="mark">群</span>
        <Loading />
      </div>
    );
  if (!authenticated)
    return (
      <Login
        onLogin={() => {
          client.clear();
          setAuthenticated(true);
        }}
      />
    );
  return (
    <div className={`app-shell ${collapsed ? "collapsed" : ""}`}>
      <aside className="sidebar">
        <a href="#overview" className="brand">
          <span className="mark">群</span>
          <span className="brand-text">
            群灵<span>QunLing</span>
          </span>
        </a>
        <button className="quick-search" onClick={() => setSearchOpen(true)}>
          <Search size={16} />
          <span>快速前往</span>
          <kbd>Ctrl K</kbd>
        </button>
        <nav aria-label="主导航">
          {pages.map(([id, label, Icon]) => (
            <a
              key={id}
              href={"#" + id}
              className={page === id ? "active" : ""}
              aria-current={page === id ? "page" : undefined}
              title={label}
            >
              <Icon size={18} />
              <span>{label}</span>
              {id === "groups" && <i />}
            </a>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="connection-label">
            <span
              className={`event-dot ${state.data?.qq_connected ? "success" : "warning"}`}
            />
            <span>
              {state.error
                ? "连接中断"
                : state.data?.qq_connected
                  ? "机器人在线"
                  : "等待连接"}
            </span>
          </div>
          <Button
            kind="ghost"
            className="collapse"
            onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? "展开导航" : "收起导航"}
          >
            {collapsed ? (
              <PanelLeftOpen size={17} />
            ) : (
              <PanelLeftClose size={17} />
            )}
            <span>收起导航</span>
          </Button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div>
            <span className="dim">管理台</span>
            <span className="slash">/</span>
            <strong>{pages.find((x) => x[0] === page)?.[1]}</strong>
          </div>
          <div className="topbar-actions">
            <span className="sync-text">
              {state.data?.qq_checked_at
                ? "更新于 " +
                  new Date(state.data.qq_checked_at * 1000).toLocaleTimeString(
                    "zh-CN",
                    { hour12: false },
                  )
                : "正在同步"}
            </span>
            <Button kind="ghost" onClick={logout} aria-label="退出登录">
              <LogOut size={16} />
              <span>退出</span>
            </Button>
          </div>
        </header>
        {logoutError && <div className="global-warning">{logoutError}</div>}
        {state.error && (
          <div className="global-warning">
            管理服务暂时断开，正在重连。已输入的内容会保留。
            <Button onClick={() => state.refetch()}>重试</Button>
          </div>
        )}
        <main className="workspace">
          {pages.map(([id]) => (
            <div className="page" key={id} hidden={page !== id}>
              {id === "overview" ? (
                <Overview navigate={navigate} active={page === id} />
              ) : id === "groups" ? (
                <Groups active={page === id} />
              ) : id === "models" ? (
                <Models active={page === id} />
              ) : id === "records" ? (
                <Records active={page === id} />
              ) : id === "knowledge" ? (
                <Knowledge active={page === id} />
              ) : (
                <System active={page === id} />
              )}
            </div>
          ))}
        </main>
      </div>
      <Drawer open={searchOpen} onOpenChange={setSearchOpen} title="快速前往">
        <SearchField
          value={query}
          onChange={setQuery}
          placeholder="搜索页面或功能"
        />
        <div className="command-list">
          {pages
            .filter((x) => x[1].includes(query))
            .map(([id, label, Icon]) => (
              <button key={id} onClick={() => navigate(id)}>
                <Icon size={18} />
                {label}
                <ArrowRight size={16} />
              </button>
            ))}
        </div>
      </Drawer>
    </div>
  );
}
class ConsoleBoundary extends React.Component<
  React.PropsWithChildren,
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="login-page">
        <div className="login-form">
          <h1>页面暂时没有载入成功</h1>
          <p>已保存的配置不受影响。可以重新载入，继续编辑。</p>
          <Button kind="primary" onClick={() => location.reload()}>
            重新载入
          </Button>
        </div>
      </main>
    );
  }
}
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <ConsoleBoundary>
      <Console />
    </ConsoleBoundary>
  </QueryClientProvider>,
);
