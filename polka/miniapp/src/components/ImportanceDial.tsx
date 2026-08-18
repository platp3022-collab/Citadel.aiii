import { haptic } from "../telegram";

const WORDS = ["мимолётное", "обычное", "стоит вернуться", "важное", "нельзя потерять"];

type Props = {
  value: number;
  onChange: (value: number) => void;
};

/**
 * Важность как лестница света: пять сегментов, заполненные ярче к правому краю.
 * Цветом не кодируем - рядом всегда стоит слово, иначе шкала нечитаема.
 */
export function ImportanceDial({ value, onChange }: Props) {
  return (
    <div>
      <div className="flex gap-1.5" role="radiogroup" aria-label="Важность">
        {[1, 2, 3, 4, 5].map((level) => {
          const active = level <= value;
          return (
            <button
              key={level}
              role="radio"
              aria-checked={value === level}
              aria-label={`${level} из 5, ${WORDS[level - 1]}`}
              onClick={() => {
                haptic.pick();
                onChange(level);
              }}
              className="h-11 flex-1 rounded-[7px] border transition-all duration-200 ease-slab active:scale-[0.97]"
              style={{
                borderColor: active ? "rgba(255,255,255,0.16)" : "rgba(255,255,255,0.06)",
                background: active
                  ? `rgba(255,255,255,${0.06 + level * 0.035})`
                  : "rgba(255,255,255,0.02)",
                boxShadow: active ? "inset 0 1px 0 rgba(255,255,255,0.18)" : "none",
              }}
            />
          );
        })}
      </div>
      <p className="mt-2.5 font-mono text-[11px] tracking-wide text-muted">
        {value}/5 · {WORDS[value - 1]}
      </p>
    </div>
  );
}
