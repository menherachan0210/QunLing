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
  minimal: "最低（minimal）",
  low: "低（low）",
  medium: "中（medium）",
  high: "高（high）",
  xhigh: "更高（xhigh）",
  max: "最高（max）",
};
type Model = {
  name: string;
  roles: string[];
  levels: string[];
  notice: string;
};

export function Reasoning({ active }: { active: boolean }) {
  const remote = useData("/api/runtime", 15000, active),
    draft = useDraft("runtime-reasoning", remote.data?.config),
    action = useAction();
  const v = draft.value;
  return (
    <div className="settings-width">
      <Section
        title="推理等级"
        description="保存后用于新消息，无需重启。更高等级通常需要等待更久。"
        action={
          <Button
            aria-label="刷新推理设置"
            disabled={action.busy}
            onClick={() => remote.refetch()}
          >
            <RefreshCw size={15} />
            刷新
          </Button>
        }
      >
        <p className="muted">
          选项来自当前模型支持的等级。共用同一模型的环节在这里一起调整。
        </p>
      </Section>
      <Notice>{remote.error && errorText(remote.error)}</Notice>
      {!v ? (
        <Loading />
      ) : (
        <>
          <fieldset
            className="settings-fieldset"
            disabled={action.busy || !!remote.error}
          >
            {remote.data?.reasoning_models?.map((model: Model) => {
              const role = model.roles[0],
                value = v.reasoning[role];
              const label = model.roles.map((key) => roles[key]).join("、");
              const saved = remote.data.config.reasoning[role];
              return (
                <Section
                  key={model.roles.join()}
                  title={label}
                  description={model.name}
                  action={<Badge>当前：{levels[saved] || saved}</Badge>}
                >
                  <Field label={label + "推理等级"}>
                    <select
                      value={value}
                      onChange={(e) =>
                        draft.setValue({
                          ...v,
                          reasoning: {
                            ...v.reasoning,
                            ...Object.fromEntries(
                              model.roles.map((key) => [key, e.target.value]),
                            ),
                          },
                        })
                      }
                    >
                      {!model.levels.includes(value) && (
                        <option value={value} disabled>
                          {levels[value] || value}（当前保留设置）
                        </option>
                      )}
                      {model.levels.map((level) => (
                        <option key={level} value={level}>
                          {levels[level] || level}
                        </option>
                      ))}
                    </select>
                  </Field>
                  {model.notice && <p className="muted">{model.notice}</p>}
                </Section>
              );
            })}
          </fieldset>
          <SaveBar
            dirty={draft.dirty}
            busy={action.busy}
            conflict={draft.conflict || !!remote.error}
            error={
              action.error || (remote.error ? errorText(remote.error) : "")
            }
            onReset={draft.reset}
            onSave={() =>
              action.run(async () => {
                const data = await api("/api/runtime", v);
                client.setQueryData(["/api/runtime"], data);
                draft.accept(data.config);
              }, "推理等级已生效，将用于新消息")
            }
          />
          {action.success && !draft.dirty && (
            <div className="saved-inline" role="status">
              <Check size={14} />
              {action.success}
            </div>
          )}
        </>
      )}
    </div>
  );
}
