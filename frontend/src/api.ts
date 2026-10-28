import { QueryClient, useQuery } from "@tanstack/react-query";
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export const client = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 5000, refetchOnWindowFocus: true },
    mutations: { retry: false },
  },
});
export async function api<T = any>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal
      ? AbortSignal.any([signal, AbortSignal.timeout(65000)])
      : AbortSignal.timeout(65000),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("管理服务返回了无法读取的结果，请稍后重试。");
  }
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("console-locked"));
    throw new ApiError(
      data.error || "暂时无法完成操作，请重试。",
      response.status,
    );
  }
  return data;
}
export function useData(path: string, interval = 0, enabled = true) {
  return useQuery({
    queryKey: [path],
    queryFn: ({ signal }) => api(path, undefined, signal),
    enabled,
    refetchInterval: interval || false,
    refetchIntervalInBackground: false,
  });
}
export const refresh = (path: string) =>
  client.invalidateQueries({ queryKey: [path] });
export const uid = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (n) =>
    n.toString(16).padStart(2, "0"),
  ).join("");
export const time = (value?: number | string) =>
  value
    ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString(
        "zh-CN",
        { hour12: false },
      )
    : "—";
export const errorText = (error: unknown) => {
  if (error instanceof TypeError) return "连接暂时中断，请检查网络后重试。";
  if (error instanceof DOMException && error.name === "TimeoutError")
    return "等待服务器响应超时，请刷新状态核对结果。";
  return error instanceof Error ? error.message : "操作未完成，请重试。";
};
