import { Check, RefreshCw } from "lucide-react";
import { api, client, errorText, useData } from "../api";
import {
  Badge,
  Button,
  Field,
  Loading,
  Notice,
  SaveBar,
  Section,
  useAction,
  useDraft,
} from "../ui";
const roles: Record<string, string> = {
  chat: "聊天",
  decision: "参与判断",
  reconcile: "内容复核",
};
const levels: Record<string, string> = {
  provider_default: "跟随服务商",
  disabled: "关闭推理",
  enabled: "启用推理",
  minimal: "最低",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "更高",
  max: "最高",
};
export function Advanced({ active }: { active: boolean }) {
  const remote = useData("/api/runtime", 15000, active),
    draft = useDraft("runtime", remote.data?.config),
    action = useAction();
  const v = draft.value;
  return (
    <>
      <Notice>{remote.error && errorText(remote.error)}</Notice>
      {!v ? (
        <Loading />
      ) : (
        <>
          <fieldset className="settings-fieldset" disabled={action.busy}>
            <Section
              title="工具与请求"
              description="更改后用于新请求，已经开始的任务按原配置执行。"
            >
              <div className="form-grid">
                <Field
                  label="最多连续工具调用"
                  hint="1–64 次；日常聊天不受这个上限影响。"
                >
                  <input
                    type="number"
                    min={1}
                    max={64}
                    value={v.tool_iterations}
                    onChange={(e) =>
                      draft.setValue({
                        ...v,
                        tool_iterations: Number(e.target.value),
                      })
                    }
                  />
                </Field>
                <Field
                  label="单次请求超时（秒）"
                  hint="30–1200 秒；具体模型和工具仍有各自超时。"
                >
                  <input
                    type="number"
                    min={30}
                    max={1200}
                    value={v.request_timeout}
                    onChange={(e) =>
                      draft.setValue({
                        ...v,
                        request_timeout: Number(e.target.value),
                      })
                    }
                  />
                </Field>
              </div>
            </Section>
            <Section
              title="推理强度"
              description="可选强度取决于服务商和模型。更高强度通常会增加等待时间。"
            >
              <div className="form-grid">
                {Object.entries(roles).map(([role, label]) => (
                  <Field key={role} label={label}>
                    <select
                      value={v.reasoning[role]}
                      onChange={(e) => {
                        const shared = remote.data.shared_roles.find(
                          (row: string[]) => row.includes(role),
                        ) || [role];
                        draft.setValue({
                          ...v,
                          reasoning: {
                            ...v.reasoning,
                            ...Object.fromEntries(
                              shared.map((key: string) => [
                                key,
                                e.target.value,
                              ]),
                            ),
                          },
                        });
                      }}
                    >
                      {Object.entries(levels).map(([id, name]) => (
                        <option key={id} value={id}>
                          {name}
                        </option>
                      ))}
                    </select>
                  </Field>
                ))}
              </div>
              {remote.data?.shared_roles
                ?.filter((row: string[]) => row.length > 1)
                .map((row: string[]) => (
                  <p className="muted" key={row.join()}>
                    {row.map((key) => roles[key]).join("、")}
                    共用同一模型，推理强度同步修改。
                  </p>
                ))}
            </Section>
          </fieldset>
          <SaveBar
            dirty={draft.dirty}
            busy={action.busy}
            conflict={draft.conflict}
            error={action.error}
            onReset={draft.reset}
            onSave={() =>
              action.run(async () => {
                const data = await api("/api/runtime", v);
                client.setQueryData(["/api/runtime"], data);
                draft.accept(data.config);
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
export function Components({ active }: { active: boolean }) {
  const remote = useData("/api/system", 30000, active);
  return (
    <Section
      title="功能组件"
      description="功能开关在“模型与能力”中统一管理。"
      action={
        <Button aria-label="刷新组件" onClick={() => remote.refetch()}>
          <RefreshCw size={15} />
        </Button>
      }
    >
      {remote.error ? (
        <Notice>{errorText(remote.error)}</Notice>
      ) : remote.data?.plugins_available === false ? (
        <Notice>组件状态暂时无法确认，请检查连接后重试。</Notice>
      ) : remote.isPending ? (
        <Loading />
      ) : (
        <div className="component-list">
          {remote.data?.plugins?.map((p: any) => (
            <div key={p.id}>
              <span>
                <strong>{p.id.split("/").at(-1)}</strong>
                <small>
                  {p.version || "版本未提供"}
                  {p.managed ? " · 群灵内置" : " · 扩展组件"}
                </small>
              </span>
              <Badge
                tone={
                  p.enabled && p.initialized
                    ? "success"
                    : p.enabled
                      ? "warning"
                      : "neutral"
                }
              >
                {!p.enabled ? "已停用" : p.initialized ? "运行正常" : "未就绪"}
              </Badge>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}
export function Storage({ active }: { active: boolean }) {
  const remote = useData("/api/system", 30000, active),
    s = remote.data?.storage;
  const gib = (n: number) => (n / 1024 ** 3).toFixed(1) + " GB";
  return (
    <Section
      title="存储空间"
      description="管理数据所在磁盘的实际用量。"
      action={
        <Button aria-label="刷新存储" onClick={() => remote.refetch()}>
          <RefreshCw size={15} />
        </Button>
      }
    >
      {remote.error ? (
        <Notice>{errorText(remote.error)}</Notice>
      ) : !s ? (
        <Loading />
      ) : (
        <>
          <div className="storage-reading">
            <strong>
              {gib(s.used)}
              <small> / {gib(s.total)}</small>
            </strong>
            <Badge tone={s.free / s.total < 0.15 ? "warning" : "neutral"}>
              {Math.round((s.used / s.total) * 100)}%
            </Badge>
          </div>
          <progress className="storage-meter" max={s.total} value={s.used} />
          <p className="muted">可用 {gib(s.free)}</p>
        </>
      )}
    </Section>
  );
}
