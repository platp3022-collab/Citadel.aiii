import { motion } from "motion/react";
import { Slab } from "../components/Slab";
import type { Shelf, Thought } from "../types";

type Props = {
  shelves: Shelf[];
  thoughts: Thought[];
  onOpen: (key: string) => void;
};

/** Полки, где ничего не лежит, не показываем: пустые ячейки читаются как ошибка вёрстки. */
export function Shelves({ shelves, thoughts, onOpen }: Props) {
  const live = thoughts.filter((thought) => thought.status !== "done");
  const filled = shelves.filter((shelf) => shelf.count > 0);

  const peak = (key: string) =>
    Math.max(1, ...live.filter((t) => t.shelf === key).map((t) => t.importance));

  if (filled.length === 0) {
    return (
      <div className="px-5 pb-40 pt-16 text-center">
        <p className="text-[17px] text-ink">Полки пустые</p>
        <p className="mx-auto mt-2 max-w-[24ch] text-[14px] leading-relaxed text-muted">
          Скажи первую мысль. Куда её положить, решу сам.
        </p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-2 px-5 pb-60">
      {filled.map((shelf, index) => {
        // Нечётный хвост растягиваем на две колонки: пустая ячейка читается как баг вёрстки.
        const wide = index === filled.length - 1 && filled.length % 2 === 1;
        return (
        <motion.div
          key={shelf.key}
          className={wide ? "col-span-2" : ""}
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: index * 0.05, ease: [0.16, 1, 0.3, 1] }}
        >
          <Slab
            depth={12 + peak(shelf.key) * 2}
            lit={peak(shelf.key)}
            skew={index % 2 === 0 ? 0 : 3}
            onClick={() => onOpen(shelf.key)}
          >
            <div className="flex flex-col justify-between p-4" style={{ height: wide ? 104 : 132 }}>
              <span className="font-mono text-[11px] tracking-widest text-faint">
                {String(shelf.count).padStart(2, "0")}
              </span>
              <div>
                <p className="text-[16px] font-medium leading-tight tracking-tight text-ink">
                  {shelf.title}
                </p>
                {shelf.subtitle && (
                  <p className="mt-1 text-[12px] leading-snug text-muted">{shelf.subtitle}</p>
                )}
              </div>
            </div>
          </Slab>
        </motion.div>
        );
      })}
    </div>
  );
}
