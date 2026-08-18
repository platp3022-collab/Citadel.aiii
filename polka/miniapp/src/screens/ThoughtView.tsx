import { useState } from "react";
import { ImportanceDial } from "../components/ImportanceDial";
import { ReminderPicker } from "../components/ReminderPicker";
import { Sheet } from "../components/Sheet";
import { api } from "../api";
import { haptic } from "../telegram";
import type { Preset, Thought } from "../types";

type Props = {
  thought: Thought;
  presets: Preset[];
  onChanged: () => void;
  onClosed: () => void;
};

export function ThoughtView({ thought, presets, onChanged, onClosed }: Props) {
  const [busy, setBusy] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [promptOpen, setPromptOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  // Сырую фразу прячем, если разбор её и так пересказывает: два одинаковых абзаца подряд читаются как сбой.
  const showRaw = thought.raw.trim() !== thought.summary.trim() &&
    thought.raw.trim() !== thought.title.trim() &&
    !thought.raw.trim().startsWith(thought.title.trim());

  async function guard(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (cause) {
      haptic.alarm();
      setError(cause instanceof Error ? cause.message : "не получилось");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="px-5 pb-14">
      <header className="pb-7 pt-2">
        <h1 className="text-[24px] font-semibold leading-tight tracking-tight text-ink">
          {thought.title}
        </h1>
        {thought.summary && (
          <p className="mt-3 text-[15px] leading-relaxed text-muted">{thought.summary}</p>
        )}
        {showRaw && (
          <div className="mt-4 border-l border-edge pl-3.5">
            <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-faint">
              как сказал
            </p>
            <p className="mt-1.5 whitespace-pre-wrap text-[13px] leading-relaxed text-faint">
              {thought.raw}
            </p>
          </div>
        )}
        {thought.tags.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-2">
            {thought.tags.map((tag) => (
              <span
                key={tag}
                className="rounded-full border border-edge px-2.5 py-1 font-mono text-[10px]
                           uppercase tracking-[0.14em] text-faint"
              >
                {tag}
              </span>
            ))}
          </div>
        )}
      </header>

      <section className="border-t border-edge py-6">
        <h2 className="mb-3.5 text-[13px] font-medium text-muted">Насколько важно</h2>
        <ImportanceDial
          value={thought.importance}
          onChange={(value) =>
            void guard(async () => {
              await api.importance(thought.id, value);
              onChanged();
            })
          }
        />
      </section>

      <section className="border-t border-edge py-6">
        <h2 className="mb-3.5 text-[13px] font-medium text-muted">Когда напомнить</h2>
        <ReminderPicker
          presets={presets}
          active={thought.reminders[0]}
          busy={busy}
          onPick={(code) =>
            void guard(async () => {
              await api.reminder(thought.id, code);
              onChanged();
            })
          }
        />
      </section>

      {error && <p className="pt-2 text-[12px] text-ember">{error}</p>}

      <section className="flex flex-col gap-2.5 border-t border-edge pt-6">
        {thought.claudeWorthy && (
          <button
            disabled={busy}
            onClick={() =>
              void guard(async () => {
                haptic.press();
                const result = await api.prompt(thought.id, true);
                setPrompt(result.prompt);
                setPromptOpen(true);
                onChanged();
              })
            }
            className="rounded-2xl border border-edge bg-white/[0.04] px-5 py-4 text-[15px] text-ink
                       transition-transform duration-200 ease-slab active:scale-[0.98] disabled:text-muted"
          >
            {busy ? "разворачиваю идею" : thought.hasPrompt ? "открыть задание" : "развернуть в задание для Claude Code"}
          </button>
        )}
        <button
          disabled={busy}
          onClick={() =>
            void guard(async () => {
              await api.status(thought.id, "done");
              haptic.done();
              onClosed();
            })
          }
          className={`rounded-2xl px-5 py-4 text-[15px] font-medium transition-transform
                      duration-200 ease-slab active:scale-[0.98]
                      ${busy ? "border border-edge bg-white/[0.06] text-muted" : "bg-ink text-void"}`}
        >
          сделано, убрать с полки
        </button>
      </section>

      <Sheet open={promptOpen} onClose={() => setPromptOpen(false)} title="Задание для Claude Code">
        <pre className="max-h-[52vh] overflow-y-auto whitespace-pre-wrap rounded-2xl border border-edge
                        bg-slab-0 p-4 font-mono text-[12px] leading-relaxed text-muted">
          {prompt}
        </pre>
        <button
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(prompt);
              setCopied(true);
              haptic.done();
              setTimeout(() => setCopied(false), 1800);
            } catch {
              setCopied(false);
            }
          }}
          className="mt-4 w-full rounded-2xl bg-ink px-5 py-4 text-[15px] font-medium text-void
                     transition-transform duration-200 ease-slab active:scale-[0.98]"
        >
          {copied ? "скопировано" : "скопировать"}
        </button>
        <p className="mt-3 text-center font-mono text-[11px] text-faint">
          файл уже прилетел в чат
        </p>
      </Sheet>
    </div>
  );
}
