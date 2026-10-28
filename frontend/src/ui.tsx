import React, { useEffect, useId, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {
  X,
  LoaderCircle,
  Check,
  AlertCircle,
  Search,
  Inbox,
} from "lucide-react";
import { errorText } from "./api";
export function Button({
  children,
  busy = false,
  kind = "secondary",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  busy?: boolean;
  kind?: string;
}) {
  return (
    <button
      {...props}
      disabled={props.disabled || busy}
      className={`button ${kind} ${className}`}
    >
      {busy && <LoaderCircle size={16} className="spin" />}
      {children}
    </button>
  );
}
export function Notice({
  children,
  tone = "error",
}: {
  children: React.ReactNode;
  tone?: string;
}) {
  return children ? (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={`notice ${tone}`}
    >
      <AlertCircle size={16} />
      <span>{children}</span>
    </div>
  ) : null;
}
export function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: string;
}) {
  return (
    <span className={`badge ${tone}`}>
      <i />
      {children}
    </span>
  );
}
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  const id = useId();
  const control =
    React.isValidElement(children) && typeof children.type === "string"
      ? React.cloneElement(children as React.ReactElement<any>, {
          id,
          "aria-describedby": hint ? id + "-hint" : undefined,
        })
      : children;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {control}
      {hint && <small id={id + "-hint"}>{hint}</small>}
    </div>
  );
}
export function Toggle({
  checked,
  onChange,
  label,
  description,
  disabled = false,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
}) {
  return (
    <label className="toggle-row">
      <span>
        <strong>{label}</strong>
        {description && <small>{description}</small>}
      </span>
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled}
      />
      <i className="switch" />
    </label>
  );
}
export function Empty({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="empty">
      <Inbox size={25} />
      <strong>{title}</strong>
      {description && <p>{description}</p>}
      {children}
    </div>
  );
}
export function Loading() {
  return (
    <div className="skeleton" role="status" aria-label="正在加载">
      <i />
      <i />
      <i />
    </div>
  );
}
export function SearchField({
  value,
  onChange,
  placeholder = "搜索",
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="search-field">
      <Search size={16} />
      <input
        aria-label={placeholder}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {value && (
        <button aria-label="清除搜索" onClick={() => onChange("")}>
          <X size={14} />
        </button>
      )}
    </div>
  );
}
export function Drawer({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay" />
        <Dialog.Content
          className="drawer-content"
          aria-describedby={description ? "drawer-description" : undefined}
        >
          <div className="drawer-heading">
            <div>
              <Dialog.Title>{title}</Dialog.Title>
              {description && (
                <Dialog.Description id="drawer-description">
                  {description}
                </Dialog.Description>
              )}
            </div>
            <Dialog.Close asChild>
              <Button aria-label="关闭" className="icon-button">
                <X size={18} />
              </Button>
            </Dialog.Close>
          </div>
          <div className="drawer-body">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
export function Section({
  title,
  description,
  children,
  action,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="section">
      <div className="section-heading">
        <div>
          <h2>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
export function PageTitle({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="page-title">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      <div className="page-actions">{children}</div>
    </div>
  );
}
function safe(value: any): any {
  if (Array.isArray(value)) return value.map(safe);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .filter(([k]) => !/(?:api_key|password|token)$/.test(k))
        .map(([k, v]) => [k, safe(v)]),
    );
  return value;
}
export function useDraft(key: string, source: any) {
  const [value, setValue] = useState<any>(null),
    [baseline, setBaseline] = useState(""),
    [revision, setRevision] = useState<any>(null),
    [restored, setRestored] = useState(false);
  const dirty = value !== null && JSON.stringify(value) !== baseline;
  const sourceRev = source?.revision ?? source?.settings_revision;
  useEffect(() => {
    if (!source) return;
    if (value === null) {
      let cached;
      try {
        cached = JSON.parse(
          sessionStorage.getItem("ql:draft:" + key) || "null",
        );
      } catch {}
      setValue(cached?.value ?? structuredClone(source));
      setBaseline(JSON.stringify(source));
      setRevision(cached?.revision ?? sourceRev);
      setRestored(!!cached);
    } else if (!dirty) {
      setValue(structuredClone(source));
      setBaseline(JSON.stringify(source));
      setRevision(sourceRev);
    }
  }, [source]);
  useEffect(() => {
    if (value === null) return;
    try {
      if (dirty)
        sessionStorage.setItem(
          "ql:draft:" + key,
          JSON.stringify({ value: safe(value), revision }),
        );
      else sessionStorage.removeItem("ql:draft:" + key);
    } catch {}
  }, [value, dirty, revision, key]);
  useEffect(() => {
    if (!dirty) return;
    const fn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", fn);
    return () => window.removeEventListener("beforeunload", fn);
  }, [dirty]);
  const accept = (next: any) => {
    setValue(structuredClone(next));
    setBaseline(JSON.stringify(next));
    setRevision(next.revision ?? next.settings_revision);
    setRestored(false);
    sessionStorage.removeItem("ql:draft:" + key);
  };
  return {
    value,
    setValue,
    dirty,
    conflict: dirty && sourceRev !== revision,
    restored,
    accept,
    reset: () => source && accept(source),
  };
}
export function SaveBar({
  dirty,
  busy,
  conflict,
  error,
  onSave,
  onReset,
  message = "修改尚未应用",
}: {
  dirty: boolean;
  busy: boolean;
  conflict?: boolean;
  error?: string;
  onSave: () => void;
  onReset: () => void;
  message?: string;
}) {
  const [discard, setDiscard] = useState(false);
  if (!dirty && !busy && !error) return null;
  return (
    <>
      <div className="save-bar">
        <div>
          {error ? (
            <span className="error-text">{error}</span>
          ) : conflict ? (
            <span className="error-text">
              设置已在其他页面变化，草稿仍保留。请重新载入后编辑。
            </span>
          ) : (
            <span>{busy ? "正在保存并确认结果…" : message}</span>
          )}
        </div>
        <Button onClick={() => setDiscard(true)} disabled={busy}>
          放弃修改
        </Button>
        <Button kind="primary" onClick={onSave} busy={busy} disabled={conflict}>
          <Check size={16} />
          应用设置
        </Button>
      </div>
      <Drawer
        open={discard}
        onOpenChange={setDiscard}
        title="放弃这次修改？"
        description="将重新载入服务器上的设置，当前草稿会被移除。"
      >
        <div className="form-actions">
          <Button onClick={() => setDiscard(false)}>继续编辑</Button>
          <Button
            kind="danger"
            onClick={() => {
              onReset();
              setDiscard(false);
            }}
          >
            确认放弃
          </Button>
        </div>
      </Drawer>
    </>
  );
}
export function useAction() {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState("");
  const run = async (fn: () => Promise<void>, message = "已保存") => {
    if (busy) return;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await fn();
      setSuccess(message);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, success, setError, run };
}
