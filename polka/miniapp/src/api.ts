import { initData } from "./telegram";
import type { State } from "./types";

/** Ключ из адресной строки нужен только для отладки в обычном браузере. */
const debugToken = new URLSearchParams(location.search).get("token") ?? "";

async function call<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (initData) headers["X-Telegram-Init-Data"] = initData;
  else if (debugToken) headers["X-Polka-Token"] = debugToken;

  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new PolkaError("Нет связи", "Проверь интернет и попробуй снова.");
  }

  // Туннель отвечает своей html-страницей, когда не достучался до Полки.
  // Показывать её человеку бессмысленно, нужен понятный текст.
  if (response.status === 530 || response.status === 502 || response.status === 504) {
    throw new PolkaError(
      "Полка не отвечает",
      "Похоже, окно с Полкой закрылось. Открой его снова, и всё вернётся.",
    );
  }
  if (response.status === 401 || response.status === 403) {
    throw new PolkaError(
      "Не пускает",
      "Открой приложение через кнопку в чате с ботом.",
    );
  }
  if (!response.ok) {
    throw new PolkaError("Полка ответила ошибкой", `Код ${response.status}.`);
  }
  if (!(response.headers.get("content-type") || "").includes("json")) {
    throw new PolkaError(
      "Ответ не от Полки",
      "Между приложением и Полкой кто-то вклинился. Перезапусти окно с Полкой.",
    );
  }
  return (await response.json()) as T;
}

/** Ошибка с коротким заголовком и человеческим объяснением. */
export class PolkaError extends Error {
  readonly hint: string;

  constructor(message: string, hint: string) {
    super(message);
    this.name = "PolkaError";
    this.hint = hint;
  }
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
