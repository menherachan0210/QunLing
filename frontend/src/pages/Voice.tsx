import { useState } from "react";
import { Check, Play, RefreshCw } from "lucide-react";
import { api, client, errorText, useData } from "../api";
import {
  Badge,
  Button,
  Field,
  Loading,
  Notice,
  SaveBar,
  Section,
  Toggle,
  useAction,
  useDraft,
} from "../ui";

export function Voice({ active }: { active: boolean }) {
  const remote = useData("/api/voice", 15000, active);
  const draft = useDraft("voice", remote.data?.config);
  const save = useAction(),
    test = useAction();
  const [key, setKey] = useState("");
  const [sample, setSample] = useState("今天群里挺热闹的，刚才聊到哪儿了？");
  const [group, setGroup] = useState("");
  const [preview, setPreview] = useState<any>(null);
  const v = draft.value,
    dirty = draft.dirty || !!key;
  const busy = save.busy || test.busy;
  const reset = () => {
    draft.reset();
    setKey("");
  };
  return (
    <div className="settings-width voice-settings">
      <Section
        title="语音"
        description="听懂群友说的话；只有被要求时，才用语音回复。保存后立即生效。"
        action={
          <Button
            aria-label="刷新语音设置"
            disabled={busy}
            onClick={() => remote.refetch()}
          >
            <RefreshCw size={15} />
            刷新
          </Button>
        }
      >
        <p className="muted">
          说什么、怎么说都沿用聊天人格。群聊使用各群的设置，私聊使用默认聊天语气。
          声音为 AI 合成；目前支持说话和朗读，不支持唱歌。
        </p>
      </Section>
      <Notice>{remote.error && errorText(remote.error)}</Notice>
      {!v ? (
        <Loading />
      ) : (
        <>
          <fieldset
            className="settings-fieldset"
            disabled={busy || !!remote.error}
          >
            <Section
              title="收听与回复"
              action={
                <Badge
                  tone={remote.data.config.enabled ? "success" : "neutral"}
                >
                  {remote.data.config.enabled ? "已开启" : "未开启"}
                </Badge>
              }
            >
              <Toggle
                label="启用语音"
                checked={v.enabled}
                onChange={(enabled) => draft.setValue({ ...v, enabled })}
              />
              <Toggle
                label="听懂语音消息"
                description="识别后按本群接话规则回应；仅 @ 回复的成员设置仍然有效。"
                checked={v.receive}
                onChange={(receive) => draft.setValue({ ...v, receive })}
              />
              <Toggle
                label="允许语音回复"
                description="群友可以说“用语音回答我”或“把这段话读出来”。平时仍用文字。"
                checked={v.reply}
                onChange={(reply) => draft.setValue({ ...v, reply })}
              />
              <Field
                label="官方语音 API Key"
                hint={
                  v.api_key_saved
                    ? "已保存。留空继续使用现有密钥；填入新密钥后保存即可替换。"
                    : "仅用于语音识别与合成，在服务器保存，不会显示给模型或群友。"
                }
              >
                <input
                  type="password"
                  autoComplete="new-password"
                  spellCheck={false}
                  maxLength={256}
                  value={key}
                  placeholder={v.api_key_saved ? "已保存，不回显" : "sk-…"}
                  onChange={(e) => setKey(e.target.value.trim())}
                />
              </Field>
            </Section>
            <Section
              title="声音"
              description="音色决定声音本身；情绪、语速和表达自动跟随当前聊天人格。"
            >
              <Field label="音色">
                <select
                  value={v.voice}
                  onChange={(e) =>
                    draft.setValue({ ...v, voice: e.target.value })
                  }
                >
                  {remote.data.voices.map((voice: string) => (
                    <option key={voice} value={voice}>
                      {voice === "marin"
                        ? "Marin（默认）"
                        : voice.charAt(0).toUpperCase() + voice.slice(1)}
                    </option>
                  ))}
                </select>
              </Field>
              <p className="muted">
                称呼和人格在“群聊”中统一修改，这里无需重复填写。
              </p>
            </Section>
            <Section title="识别设置">
              <Field
                label="额外识别纠错词（可选）"
                hint="机器人称呼、当前群名和发言者昵称会自动读取。这里只补充容易听错的作品名或术语，每行一个，最多 30 行；不是机器人名称。"
              >
                <textarea
                  rows={3}
                  maxLength={500}
                  value={v.keywords}
                  onChange={(e) =>
                    draft.setValue({ ...v, keywords: e.target.value })
                  }
                />
              </Field>
              <Field
                label="单条收听时长（秒）"
                hint="15～180 秒，默认 120 秒；音频文件最大 12 MiB。超过时会提示分段，不会假装听完。"
              >
                <input
                  type="number"
                  min={15}
                  max={180}
                  step={1}
                  value={v.max_seconds}
                  onChange={(e) =>
                    draft.setValue({
                      ...v,
                      max_seconds: Number(e.target.value),
                    })
                  }
                />
              </Field>
              <p className="muted">
                语音回复每条最多 600 字、60 秒。识别与合成按官方 API 用量计费。
              </p>
            </Section>
          </fieldset>
          {(dirty || save.busy || save.error) && (
            <SaveBar
              dirty={dirty}
              busy={busy}
              conflict={draft.conflict || !!remote.error}
              error={save.error}
              onReset={reset}
              onSave={() =>
                save.run(async () => {
                  const { api_key_saved: _, ...config } = v;
                  const data = await api("/api/voice", {
                    ...config,
                    api_key: key,
                  });
                  client.setQueryData(["/api/voice"], data);
                  draft.accept(data.config);
                  setKey("");
                  setPreview(null);
                }, "语音设置已生效")
              }
            />
          )}
          {save.success && !dirty && (
            <div className="saved-inline" role="status">
              <Check size={14} />
              {save.success}
            </div>
          )}
          <Section
            title="试听与识别测试"
            description="使用已保存的设置合成一段声音，再识别回来。仅在本页试听，不发送到 QQ。"
          >
            <Field label="试听人格" hint="群聊人格修改并保存后，在这里选择对应群试听。">
              <select value={group} disabled={busy} onChange={(e) => { setGroup(e.target.value); setPreview(null); }}>
                <option value="">默认聊天语气（私聊）</option>
                {(remote.data.contexts || []).map((context: any) => (
                  <option key={context.group_id} value={context.group_id}>
                    {context.display_name} · 群 {context.group_id}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="试听文字"
              hint="最多 100 字，点击测试会调用一次语音合成和一次语音识别。"
            >
              <textarea
                rows={2}
                maxLength={100}
                disabled={busy}
                value={sample}
                onChange={(e) => setSample(e.target.value)}
              />
            </Field>
            <Button
              kind="primary"
              busy={test.busy}
              disabled={
                busy ||
                dirty ||
                draft.conflict ||
                !!remote.error ||
                !v.api_key_saved ||
                !sample.trim()
              }
              onClick={() =>
                test.run(async () => {
                  setPreview(null);
                  setPreview(
                    await api("/api/voice/test", {
                      revision: v.revision,
                      text: sample.trim(),
                      group_id: group,
                      chat_revision: remote.data.chat_revision,
                    }),
                  );
                })
              }
            >
              <Play size={15} />
              试听并验证识别
            </Button>
            {dirty && <p className="muted">请先保存设置，再测试声音。</p>}
            <Notice>{test.error}</Notice>
            {preview && (
              <div className="voice-preview" aria-live="polite">
                <p>
                  AI 合成语音 · 生成 {preview.speech_seconds} 秒 · 识别{" "}
                  {preview.transcription_seconds} 秒
                </p>
                <audio
                  controls
                  preload="metadata"
                  aria-label="语音试听"
                  src={`data:audio/wav;base64,${preview.audio_base64}`}
                />
                <p>
                  <strong>识别结果：</strong>
                  {preview.transcript}
                </p>
              </div>
            )}
          </Section>
        </>
      )}
    </div>
  );
}
