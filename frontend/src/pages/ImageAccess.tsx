import { useMemo } from "react";
import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, client, errorText, useData } from "../api";
import {
  Badge,
  Button,
  Empty,
  Field,
  Loading,
  Notice,
  SaveBar,
  Section,
  useAction,
  useDraft,
} from "../ui";

type Member = {
  user_id: string;
  group_id: string;
  note: string;
  reply_mode: "persona" | "text";
  reply_text: string;
};
const emptyMember = (): Member => ({
  user_id: "",
  group_id: "",
  note: "",
  reply_mode: "persona",
  reply_text: "",
});

export function ImageAccess({ active }: { active: boolean }) {
  const remote = useData("/api/proactive", 15000, active);
  const groups = useData("/api/proactive/groups", 30000, active);
  const source = useMemo(
    () =>
      remote.data?.config
        ? {
            revision: remote.data.config.revision,
            image_access: remote.data.config.image_access || {
              blocked_members: [],
            },
          }
        : null,
    [remote.data?.config],
  );
  const draft = useDraft("image-access", source),
    action = useAction();
  const members: Member[] = draft.value?.image_access.blocked_members || [];
  const update = (next: Member[]) =>
    draft.setValue({ ...draft.value, image_access: { blocked_members: next } });
  const change = (index: number, patch: Partial<Member>) =>
    update(members.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  const joined: { group_id: string; name: string }[] =
    groups.data?.groups || [];
  const save = () =>
    action.run(async () => {
      if (!remote.data?.config || remote.error || remote.data.valid === false)
        throw Error("请先刷新并成功读取生图权限，再保存修改。");
      if (members.some((row) => !/^[1-9][0-9]{0,19}$/.test(row.user_id)))
        throw Error("请填写有效的成员 QQ 号。");
      if (
        members.some(
          (row) => row.reply_mode === "text" && !row.reply_text.trim(),
        )
      )
        throw Error("选择固定台词的成员需要填写回复内容。");
      if (
        new Set(members.map((row) => row.user_id + ":" + row.group_id)).size !==
        members.length
      )
        throw Error("同一个成员在相同范围内只能添加一次。");
      const result = await api("/api/proactive", {
        ...remote.data.config,
        ...draft.value,
      });
      client.setQueryData(["/api/proactive"], result);
      draft.accept({
        revision: result.config.revision,
        image_access: result.config.image_access,
      });
    }, "生图权限已生效");
  return (
    <div className="settings-width">
      <Section
        title="生图黑名单"
        description="按真实 QQ 号限制生成和编辑图片，包括 GIF。日常聊天与识图照常。"
        action={
          <Button
            aria-label="刷新生图权限"
            disabled={action.busy}
            onClick={() => remote.refetch()}
          >
            <RefreshCw size={15} />
            刷新
          </Button>
        }
      >
        <div className="inline-heading">
          <Badge>{members.length} 条成员规则</Badge>
          <Button
            disabled={
              !draft.value ||
              action.busy ||
              members.length >= 500 ||
              !!remote.error ||
              remote.data?.valid === false
            }
            onClick={() => update([...members, emptyMember()])}
          >
            <Plus size={15} />
            添加成员
          </Button>
        </div>
        <p className="muted">
          保存后立即生效。被限制的成员尝试生图时，按当前人格嘲讽一句；也可以为他设置专属台词。
        </p>
      </Section>
      <Notice>{remote.error && errorText(remote.error)}</Notice>
      {remote.data?.valid === false && (
        <Notice>权限设置暂不可读，请恢复服务器配置后刷新。</Notice>
      )}
      <Notice tone="success">{action.success}</Notice>
      {!draft.value ? (
        <Loading />
      ) : (
        <>
          <fieldset
            className="settings-fieldset"
            disabled={
              action.busy || !!remote.error || remote.data?.valid === false
            }
          >
            {!members.length && (
              <Empty
                title="还没有被限制的成员"
                description="添加 QQ 号，设置适用范围和被拦截时的回复。"
              />
            )}
            {members.map((row, index) => (
              <Section
                key={index}
                title={
                  row.note || (row.user_id ? "QQ " + row.user_id : "新成员")
                }
                action={
                  <Button
                    kind="ghost"
                    aria-label={"移除成员规则 " + (index + 1)}
                    onClick={() =>
                      update(members.filter((_, i) => i !== index))
                    }
                  >
                    <Trash2 size={15} />
                    移除
                  </Button>
                }
              >
                <div className="form-grid">
                  <Field label="成员 QQ 号">
                    <input
                      inputMode="numeric"
                      maxLength={20}
                      value={row.user_id}
                      onChange={(e) =>
                        change(index, { user_id: e.target.value.trim() })
                      }
                    />
                  </Field>
                  <Field label="备注" hint="仅后台可见，不会发给模型或群友。">
                    <input
                      maxLength={80}
                      value={row.note}
                      onChange={(e) => change(index, { note: e.target.value })}
                    />
                  </Field>
                  <Field label="生效范围">
                    <select
                      value={row.group_id}
                      onChange={(e) =>
                        change(index, { group_id: e.target.value })
                      }
                    >
                      <option value="">所有群和私聊</option>
                      {joined.map((group) => (
                        <option key={group.group_id} value={group.group_id}>
                          {group.name}（{group.group_id}）
                        </option>
                      ))}
                      {row.group_id &&
                        !joined.some(
                          (group) => group.group_id === row.group_id,
                        ) && (
                          <option value={row.group_id}>
                            群 {row.group_id}（未在当前群列表）
                          </option>
                        )}
                    </select>
                  </Field>
                  <Field label="拦截时怎么回复">
                    <select
                      value={row.reply_mode}
                      onChange={(e) =>
                        change(index, {
                          reply_mode: e.target.value as Member["reply_mode"],
                        })
                      }
                    >
                      <option value="persona">跟随人格嘲讽</option>
                      <option value="text">固定台词</option>
                    </select>
                  </Field>
                </div>
                {row.reply_mode === "text" ? (
                  <Field
                    label="专属回复"
                    hint="只在这个成员尝试生图时回复，最多 300 字。"
                  >
                    <textarea
                      rows={3}
                      maxLength={300}
                      placeholder="生图资格都没了，还搁这儿点菜呢。"
                      value={row.reply_text}
                      onChange={(e) =>
                        change(index, { reply_text: e.target.value })
                      }
                    />
                  </Field>
                ) : (
                  <p className="muted">
                    群聊沿用本群人格，私聊沿用默认语气；只围绕这次生图请求接一句梗。
                  </p>
                )}
              </Section>
            ))}
          </fieldset>
          {groups.error && (
            <Notice tone="neutral">
              群列表暂时无法载入，仍可设置所有群和私聊的规则。
            </Notice>
          )}
          <SaveBar
            dirty={draft.dirty}
            busy={action.busy}
            conflict={draft.conflict}
            error={action.error}
            onSave={save}
            onReset={draft.reset}
          />
        </>
      )}
    </div>
  );
}
