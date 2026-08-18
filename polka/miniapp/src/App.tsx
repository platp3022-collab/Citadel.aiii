import { useCallback, useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { api } from "./api";
import { CaptureSheet } from "./components/CaptureSheet";
import { Shelves } from "./screens/Shelves";
import { ShelfView } from "./screens/ShelfView";
import { ThoughtView } from "./screens/ThoughtView";
import { backButton, boot, haptic } from "./telegram";
import type { State } from "./types";

type View =
  | { name: "shelves" }
  | { name: "shelf"; key: string }
  | { name: "thought"; id: string };

export default function App() {
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [view, setView] = useState<View>({ name: "shelves" });
  const [capturing, setCapturing] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setState(await api.state());
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "не читается");
    }
  }, []);

  useEffect(() => {
    boot();
    void refresh();
  }, [refresh]);

  const goBack = useCallback(() => {
    haptic.tap();
    setView((current) =>
      current.name === "thought" ? { name: "shelf", key: thoughtShelf(current.id) } : { name: "shelves" },
    );
    function thoughtShelf(id: string): string {
      return state?.thoughts.find((thought) => thought.id === id)?.shelf ?? "inbox";
    }
  }, [state]);

  useEffect(() => backButton(view.name !== "shelves", goBack), [view, goBack]);

  const shelf = useMemo(
    () => (view.name === "shelf" ? state?.shelves.find((s) => s.key === view.key) : undefined),
    [view, state],
  );
  const thought = useMemo(
    () => (view.name === "thought" ? state?.thoughts.find((t) => t.id === view.id) : undefined),
    [view, state],
  );

  const pending = state?.thoughts.filter((t) => t.status !== "done").length ?? 0;

  return (
    <div className="min-h-[100dvh]">
      <div className="grain" aria-hidden />

      {view.name === "shelves" && (
        <header className="px-5 pb-8 pt-7">
          <h1 className="text-[30px] font-semibold tracking-tight text-ink">Полка</h1>
          <p className="mt-1.5 text-[14px] text-muted">
            {pending > 0 ? `${pending} на местах` : "пока пусто"}
          </p>
        </header>
      )}

      {error && (
        <div className="mx-5 mb-4 rounded-2xl border border-edge bg-white/[0.03] px-4 py-3">
          <p className="text-[13px] text-ink">Не дотянулся до полок</p>
          <p className="mt-1 font-mono text-[11px] text-faint">{error}</p>
          <button onClick={() => void refresh()} className="mt-2.5 text-[13px] underline">
            попробовать снова
          </button>
        </div>
      )}

      {!state && !error && <Skeleton />}

      <AnimatePresence mode="wait">
        <motion.main
          key={view.name + ("key" in view ? view.key : "") + ("id" in view ? view.id : "")}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -6 }}
          transition={{ duration: 0.28, ease: [0.32, 0.72, 0, 1] }}
        >
          {state && view.name === "shelves" && (
            <Shelves
              shelves={state.shelves}
              thoughts={state.thoughts}
              onOpen={(key) => {
                haptic.tap();
                setView({ name: "shelf", key });
              }}
            />
          )}
          {state && view.name === "shelf" && shelf && (
            <ShelfView
              shelf={shelf}
              thoughts={state.thoughts}
              onOpen={(id) => {
                haptic.tap();
                setView({ name: "thought", id });
              }}
            />
          )}
          {state && view.name === "thought" && thought && (
            <ThoughtView
              thought={thought}
              presets={state.presets}
              onChanged={() => void refresh()}
              onClosed={() => {
                void refresh();
                setView({ name: "shelf", key: thought.shelf });
              }}
            />
          )}
        </motion.main>
      </AnimatePresence>

      {/* На карточке мысли главное действие своё, вторая плавающая кнопка спорила бы с ним. */}
      {view.name !== "thought" && (
      <div className="pointer-events-none fixed inset-x-0 bottom-0 z-20 flex justify-center
                      bg-gradient-to-t from-void via-void/90 to-transparent px-5 pb-7 pt-10">
        <button
          onClick={() => {
            haptic.press();
            setCapturing(true);
          }}
          className="pointer-events-auto flex w-full max-w-sm items-center justify-between gap-3
                     rounded-full border border-edge-lit bg-slab-2 py-2 pl-6 pr-2
                     shadow-[0_14px_40px_rgba(0,0,0,0.6),inset_0_1px_0_rgba(255,255,255,0.16)]
                     transition-transform duration-200 ease-slab active:scale-[0.98]"
        >
          <span className="text-[15px] text-ink">записать мысль</span>
          <span className="flex h-11 w-11 items-center justify-center rounded-full bg-ink text-[20px] leading-none text-void">
            +
          </span>
        </button>
      </div>
      )}

      <CaptureSheet
        open={capturing}
        onClose={() => setCapturing(false)}
        onSend={async (text) => {
          await api.capture(text);
          await refresh();
        }}
      />
    </div>
  );
}

function Skeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 px-5" aria-hidden>
      {[0, 1, 2, 3].map((index) => (
        <div
          key={index}
          className="h-[132px] animate-pulse rounded-[18px] border border-edge bg-slab-0"
          style={{ animationDelay: `${index * 90}ms` }}
        />
      ))}
    </div>
  );
}
