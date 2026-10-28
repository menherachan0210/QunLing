import React, { useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Eye,
  EyeOff,
  Play,
  RefreshCw,
  Server,
  SlidersHorizontal,
  Zap,
} from "lucide-react";
import { api, client, errorText, uid, useData } from "../api";
import {
  Badge,
  Button,
  Field,
  Loading,
  Notice,
  PageTitle,
  SaveBar,
  Section,
  Toggle,
  useAction,
  useDraft,
} from "../ui";
const roles = [
  ["chat", "聊天", "生成日常回复"],
  ["decision", "参与判断", "判断什么时候接话"],
  ["reconcile", "内容复核", "核对意图、事实与表达"],
  ["vision", "视觉", "理解图片与视觉资料"],
  ["image", "图片", "生成和编辑图片"],
  ["search", "联网", "搜索与整理公开资料"],
] as const;
const features = {
  memory: ["长期记忆", "记住群内成员、互动约定和相关往事"],
  files: ["文件工作区", "处理、保存和交付文件"],
  vision: ["视觉理解", "理解图片和群资料"],
  image: ["图片生成与编辑", "使用图片模型完成创作"],
  search: ["联网搜索", "按需要查找与核实资料"],
  comic: ["漫画翻译", "识别画面文字并翻译"],
};
const modelKey = (role: string) =>
  role === "chat" ? "chat_model" : role + "_model";
const endpointKey = (role: string) =>
  role === "chat" ? "base_url" : role + "_base_url";
const secretKey = (role: string) =>
  role === "chat" ? "api_key" : role + "_api_key";
function Secret({
  value,
  onChange,
  saved,
}: {
  value: string;
  onChange: (v: string) => void;
  saved: boolean;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="secret-field">
      <input
        aria-label="API Key"
        type={visible ? "text" : "password"}
        autoComplete="off"
        value={value || ""}
        placeholder={saved ? "已保存 · 留空保留" : "填写服务商提供的密钥"}
        onChange={(e) => onChange(e.target.value)}
      />
      <button
        aria-label={visible ? "隐藏密钥" : "显示密钥"}
        type="button"
        onClick={() => setVisible(!visible)}
      >
        {visible ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
    </div>
  );
}
export function Models({ active }: { active: boolean }) {
  const state = useData("/api/status", 5000, active),
    tests = useData("/api/capabilities", 3000, active);
  const source = useMemo(
    () =>
      state.data
        ? {
            features: [],
            ...state.data.settings,
            settings_revision: state.data.settings_revision,
          }
        : null,
    [state.data?.settings_revision, state.data?.configured],
  );
  const draft = useDraft("models", source),
    action = useAction(),
    probe = useAction();
  const [tab, setTab] = useState("models"),
    [expanded, setExpanded] = useState("chat"),
    [checks, setChecks] = useState(["chat"]),
    [ack, setAck] = useState(false),
    [keyDirty, setKeyDirty] = useState(false);
  const v = draft.value;
  const edit = (key: string, value: any) => {
    draft.setValue({ ...v, [key]: value });
    if (key.endsWith("api_key")) setKeyDirty(true);
  };
  const save = () =>
    action.run(async () => {
      const payload = Object.fromEntries(
        Object.entries(v).filter(([key]) => !key.endsWith("_saved")),
      );
      const receipt = await api("/api/configure", payload);
      let current;
      for (let attempt = 0; attempt < 120; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        current = await api("/api/status");
        client.setQueryData(["/api/status"], current);
        if (current.stage === "error")
          throw Error(current.message || "配置应用失败，输入已保留。");
        if (
          current.stage !== "configuring" &&
          current.configured &&
          (receipt.target_revision
            ? current.settings_revision === receipt.target_revision
            : current.settings_revision !== v.settings_revision)
        ) {
          draft.accept({
            ...current.settings,
            settings_revision: current.settings_revision,
          });
          setKeyDirty(false);
          return;
        }
      }
      throw Error(
        "配置已提交，尚未确认应用完成。请检查运行状态，不要重复提交。",
      );
    }, "配置已应用");
  return (
    <>
      <PageTitle
        title="模型与能力"
        description="明确每个模型的分工，让配置更容易理解。"
      >
        <Badge tone={state.data?.configured ? "success" : "neutral"}>
          {state.data?.configured ? "已配置" : "待配置"}
        </Badge>
      </PageTitle>
      <div className="tabs">
        {[
          ["models", "模型分工"],
          ["features", "功能能力"],
          ["parameters", "工具与上下文"],
          ["test", "连接测试"],
        ].map(([id, label]) => (
          <button key={id} aria-pressed={tab === id} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </div>
      {state.error && <Notice>{errorText(state.error)}</Notice>}
      {!v ? (
        <Loading />
      ) : (
        <>
          <fieldset className="settings-fieldset" disabled={action.busy}>
            {tab === "models" ? (
              <div className="model-list">
                {roles.map(([role, label, description]) => (
                  <section
                    className={
                      "model-row " + (expanded === role ? "expanded" : "")
                    }
                    key={role}
                  >
                    <button
                      className="model-heading"
                      onClick={() => setExpanded(expanded === role ? "" : role)}
                    >
                      <span className="model-icon">
                        {role === "decision" ? (
                          <Zap size={19} />
                        ) : (
                          <Server size={19} />
                        )}
                      </span>
                      <span className="grow">
                        <strong>{label}</strong>
                        <small>{description}</small>
                      </span>
                      <span className="model-name">
                        {v[modelKey(role)] ||
                          (role === "image" ? "尚未设置" : "跟随聊天模型")}
                      </span>
                      <Badge>
                        {role === "chat" || v[endpointKey(role)]
                          ? "独立服务"
                          : "共用服务"}
                      </Badge>
                      {expanded === role ? (
                        <ChevronDown size={16} />
                      ) : (
                        <ChevronRight size={16} />
                      )}
                    </button>
                    {expanded === role && (
                      <div className="model-fields">
                        <Field
                          label="模型名称"
                          hint={
                            role === "chat"
                              ? "填写服务商支持的准确模型名。"
                              : "留空时跟随聊天模型，图片模型需单独填写。"
                          }
                        >
                          <input
                            value={v[modelKey(role)] || ""}
                            onChange={(e) =>
                              edit(modelKey(role), e.target.value)
                            }
                            placeholder={
                              role === "chat"
                                ? "例如模型服务中的名称"
                                : "跟随聊天模型"
                            }
                          />
                        </Field>
                        <Field
                          label={role === "chat" ? "服务地址" : "独立服务地址"}
                          hint={
                            role === "chat"
                              ? "兼容 OpenAI 的 HTTP(S) 接口地址。"
                              : "留空共用聊天服务；更换地址时需填写对应密钥。"
                          }
                        >
                          <input
                            type="url"
                            value={v[endpointKey(role)] || ""}
                            placeholder={
                              role === "chat"
                                ? "https://api.example.com/v1"
                                : "共用聊天服务"
                            }
                            onChange={(e) =>
                              edit(endpointKey(role), e.target.value)
                            }
                          />
                        </Field>
                        <Field label="API Key">
                          <Secret
                            value={v[secretKey(role)] || ""}
                            saved={!!v[secretKey(role) + "_saved"]}
                            onChange={(value) => edit(secretKey(role), value)}
                          />
                        </Field>
                      </div>
                    )}
                  </section>
                ))}
              </div>
            ) : tab === "features" ? (
              <div className="settings-width">
                <Section
                  title="按需启用"
                  description="关闭功能会保留已有数据。"
                >
                  {Object.entries(features).map(
                    ([key, [label, description]]) => (
                      <Toggle
                        key={key}
                        label={label}
                        description={description}
                        checked={(v.features || []).includes(key)}
                        onChange={(checked) =>
                          edit(
                            "features",
                            checked
                              ? [...new Set([...(v.features || []), key])]
                              : v.features.filter((x: string) => x !== key),
                          )
                        }
                      />
                    ),
                  )}
                </Section>
                <Notice tone="neutral">
                  图片、联网和漫画需要文件工作区；漫画同时需要视觉理解，保存时会补齐这些依赖。
                </Notice>
              </div>
            ) : tab === "parameters" ? (
              <div className="settings-width">
                <Section
                  title="近期群聊范围"
                  description="控制近期理解范围，不建立永久聊天档案。"
                >
                  <div className="form-grid">
                    <Field label="回看时长（小时）">
                      <input
                        type="number"
                        min={1}
                        max={168}
                        value={v.group_context_hours ?? 24}
                        onChange={(e) =>
                          edit("group_context_hours", Number(e.target.value))
                        }
                      />
                    </Field>
                    <Field label="最多消息数">
                      <input
                        type="number"
                        min={100}
                        max={1000}
                        value={v.group_context_max_messages ?? 1000}
                        onChange={(e) =>
                          edit(
                            "group_context_max_messages",
                            Number(e.target.value),
                          )
                        }
                      />
                    </Field>
                  </div>
                </Section>
                <Section title="图片参数">
                  <div className="form-grid">
                    <Field label="尺寸">
                      <select
                        value={v.image_size || "1024x1024"}
                        onChange={(e) => edit("image_size", e.target.value)}
                      >
                        {[
                          "1024x1024",
                          "1280x1280",
                          "768x1280",
                          "1280x768",
                          "720x1280",
                          "1280x720",
                          "1024x768",
                          "768x1024",
                        ].map((x) => (
                          <option key={x}>{x}</option>
                        ))}
                      </select>
                    </Field>
                    <Field label="画质">
                      <select
                        value={v.image_quality || "auto"}
                        onChange={(e) => edit("image_quality", e.target.value)}
                      >
                        {["auto", "low", "medium", "high", "xhigh", "max"].map(
                          (x) => (
                            <option key={x}>{x}</option>
                          ),
                        )}
                      </select>
                    </Field>
                    <Field label="图片超时（秒）">
                      <input
                        type="number"
                        min={30}
                        max={300}
                        value={v.image_timeout_seconds ?? 180}
                        onChange={(e) =>
                          edit("image_timeout_seconds", Number(e.target.value))
                        }
                      />
                    </Field>
                    <Field label="搜索超时（秒）">
                      <input
                        type="number"
                        min={30}
                        max={90}
                        value={v.search_timeout_seconds ?? 60}
                        onChange={(e) =>
                          edit("search_timeout_seconds", Number(e.target.value))
                        }
                      />
                    </Field>
                  </div>
                </Section>
              </div>
            ) : (
              <div className="settings-width">
                <Section
                  title="验证已保存的配置"
                  description="逐项调用服务，显示真实响应结果和耗时。"
                >
                  <div className="checks">
                    {[
                      ["chat", "聊天与工具"],
                      ["vision", "视觉理解"],
                      ["search", "联网搜索"],
                      ["image", "图片生成"],
                    ].map(([key, label]) => (
                      <label key={key}>
                        <input
                          type="checkbox"
                          checked={checks.includes(key)}
                          onChange={(e) =>
                            setChecks(
                              e.target.checked
                                ? [...checks, key]
                                : checks.filter((x) => x !== key),
                            )
                          }
                        />
                        {label}
                      </label>
                    ))}
                  </div>
                  <label className="check-line">
                    <input
                      type="checkbox"
                      checked={ack}
                      onChange={(e) => setAck(e.target.checked)}
                    />
                    我知道测试会调用模型，并可能产生服务费用
                  </label>
                  <div className="form-actions">
                    <Button
                      kind="primary"
                      busy={probe.busy || tests.data?.job?.status === "running"}
                      disabled={
                        !ack || !checks.length || draft.dirty || keyDirty
                      }
                      onClick={() =>
                        probe.run(async () => {
                          await api("/api/capabilities", {
                            checks,
                            acknowledge_cost: true,
                            request_id: uid(),
                          });
                          setAck(false);
                          await tests.refetch();
                        }, "测试已开始")
                      }
                    >
                      <Play size={15} />
                      开始测试
                    </Button>
                    <Button onClick={() => tests.refetch()}>
                      <RefreshCw size={15} />
                      查看结果
                    </Button>
                  </div>
                  {(draft.dirty || keyDirty) && (
                    <Notice tone="warning">先应用修改，再测试当前配置。</Notice>
                  )}
                  <Notice>
                    {probe.error || (tests.error && errorText(tests.error))}
                  </Notice>
                  <div className="test-results">
                    {tests.data?.job?.results?.map((row: any) => (
                      <div key={row.check}>
                        <Badge
                          tone={
                            row.status === "passed"
                              ? "success"
                              : row.status === "failed"
                                ? "error"
                                : "warning"
                          }
                        >
                          {row.status === "passed"
                            ? "通过"
                            : row.status === "failed"
                              ? "未通过"
                              : "未确认"}
                        </Badge>
                        <strong>{row.check}</strong>
                        <span>{row.seconds} 秒</span>
                        <p>{row.message}</p>
                      </div>
                    ))}
                  </div>
                </Section>
              </div>
            )}
          </fieldset>
          <SaveBar
            dirty={draft.dirty || keyDirty}
            busy={action.busy}
            conflict={draft.conflict}
            error={action.error}
            onSave={save}
            onReset={() => {
              draft.reset();
              setKeyDirty(false);
            }}
          />
          {action.success && !draft.dirty && !keyDirty && (
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
