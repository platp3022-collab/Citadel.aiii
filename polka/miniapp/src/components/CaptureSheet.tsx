import { useEffect, useRef, useState } from "react";
import { Sheet } from "./Sheet";
import { haptic } from "../telegram";

type Props = {
  open: boolean;
  onClose: () => void;
  onSend: (text: string) => Promise<void>;
};

export function CaptureSheet({ open, onClose, onSend }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const field = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (open) {
      setError("");
      // Пауза даёт шторке доехать, иначе клавиатура ломает анимацию.
      const timer = setTimeout(() => field.current?.focus(), 320);
      return () => clearTimeout(timer);
    }
    setText("");
    return undefined;
  }, [open]);

  async function send() {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setError("");
    try {
      await onSend(value);
      haptic.done();
      setText("");
      onClose();
    } catch (cause) {
      haptic.alarm();
      setError(cause instanceof Error ? cause.message : "не отправилось");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet open={open} onClose={onClose} title="Что в голове">
      <label className="sr-only" htmlFor="thought">
        Текст мысли
      </label>
      <textarea
        id="thought"
        ref={field}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void send();
        }}
        rows={4}
        placeholder="говори как думаешь, разберу сам"
        className="w-full resize-none rounded-2xl border border-edge bg-slab-0 px-4 py-3.5 text-[15px]
                   leading-relaxed text-ink placeholder:text-faint focus:border-white/20 focus:outline-none"
      />
      {error && <p className="mt-2 text-[12px] text-ember">{error}</p>}
      <button
        onClick={send}
        disabled={busy || !text.trim()}
        className={`mt-4 flex w-full items-center justify-center gap-2 rounded-2xl px-5 py-4
                    text-[15px] font-medium transition-transform duration-200 ease-slab active:scale-[0.98]
                    ${busy || !text.trim()
                      ? "border border-edge bg-white/[0.06] text-muted"
                      : "bg-ink text-void"}`}
      >
        {busy ? "раскладываю" : "на полку"}
      </button>
    </Sheet>
  );
}
