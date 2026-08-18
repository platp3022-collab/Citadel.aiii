import { motion } from "motion/react";
import { Slab } from "../components/Slab";
import type { Shelf, Thought } from "../types";

type Props = {
  shelf: Shelf;
  thoughts: Thought[];
  onOpen: (id: string) => void;
};

function ago(seconds: number): string {
  const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
  if (minutes < 60) return `${Math.max(1, minutes)} мин назад`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} ч назад`;
  return `${Math.round(minutes / 60 / 24)} дн назад`;
}

export function ShelfView({ shelf, thoughts, onOpen }: Props) {
  const live = thoughts
    .filter((thought) => thought.shelf === shelf.key && thought.status !== "done")
    .sort((a, b) => b.importance - a.importance || b.createdAt - a.createdAt);

  return (
    <div className="px-5 pb-60">
      <header className="pb-6 pt-2">
        <h1 className="text-[26px] font-semibold tracking-tight text-ink">{shelf.title}</h1>
        {shelf.subtitle && <p className="mt-1 text-[14px] text-muted">{shelf.subtitle}</p>}
      </header>

      {live.length === 0 ? (
        <p className="pt-8 text-center text-[14px] text-muted">Здесь пусто. Всё закрыто.</p>
      ) : (
        <div className="flex flex-col gap-3.5">
          {live.map((thought, index) => (
            <motion.div
              key={thought.id}
              initial={{ opacity: 0, y: 14 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.45, delay: index * 0.04, ease: [0.16, 1, 0.3, 1] }}
            >
              <Slab
                depth={7 + thought.importance * 2}
                lit={thought.importance}
                radius={16}
                onClick={() => onOpen(thought.id)}
              >
                <div className="p-4">
                  <div className="flex items-baseline justify-between gap-3">
                    <p className="text-[15px] font-medium leading-snug tracking-tight text-ink">
                      {thought.title}
                    </p>
                    <span className="shrink-0 font-mono text-[11px] text-faint">
                      {thought.importance}/5
                    </span>
                  </div>
                  {thought.summary && (
                    <p className="mt-1.5 text-[13px] leading-relaxed text-muted">
                      {thought.summary}
                    </p>
                  )}
                  <div className="mt-3 flex items-center gap-3 font-mono text-[10.5px] text-faint">
                    <span>{ago(thought.createdAt)}</span>
                    {thought.reminders.length > 0 && <span>напоминание стоит</span>}
                  </div>
                </div>
              </Slab>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}
