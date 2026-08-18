import { initData } from "./telegram";
import type { State } from "./types";

/** Ключ из адресной строки нужен только для отладки в обычном браузере. */
const debugToken = new URLSearchParams(location.search).get("token") ?? "";

async function call<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (initData) headers["X-Telegram-Init-Data"] = initData;
  else if (debugToken) headers["X-Polka-Token"] = debugToken;

  const response = await fetch(`/api${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${await response.text()}`);
  }
  return (await response.json()) as T;
}

export const api = {
  state: () => call<State>("/state"),
  capture: (text: string) =>
    call<{ id: string; title: string; shelf: string }>("/capture", { text }),
  importance: (id: string, importance: number) =>
    call<{ ok: true }>(`/thoughts/${id}/importance`, { importance }),
  reminder: (id: string, code: string) =>
    call<{ label: string }>(`/thoughts/${id}/reminder`, { code }),
  status: (id: string, status: "shelved" | "done" | "deleted") =>
    call<{ ok: true }>(`/thoughts/${id}/status`, { status }),
  prompt: (id: string, send = false) =>
    call<{ prompt: string }>(`/thoughts/${id}/prompt${send ? "?send=1" : ""}`, {}),
};
