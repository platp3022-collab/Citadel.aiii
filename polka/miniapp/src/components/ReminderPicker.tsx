import { haptic } from "../telegram";
import type { Preset, Reminder } from "../types";

type Props = {
  presets: Preset[];
  active: Reminder | undefined;
  onPick: (code: string) => void;
  busy: boolean;
};

function whenText(reminder: Reminder, now: number): string {
  const minutes = Math.round((reminder.nextFireAt - now) / 60);
  const every =
    reminder.mode === "every"
      ? `, потом каждые ${reminder.intervalMin >= 1440
          ? `${Math.round(reminder.intervalMin / 1440)} дн`
          : `${Math.round(reminder.intervalMin / 60)} ч`}`
      : "";
  if (minutes <= 1) return `вот-вот${every}`;
  if (minutes < 60) return `через ${minutes} мин${every}`;
  if (minutes < 60 * 24) return `через ${Math.round(minutes / 60)} ч${every}`;
  return `через ${Math.round(minutes / 60 / 24)} дн${every}`;
}

export function ReminderPicker({ presets, active, onPick, busy }: Props) {
  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {presets.map((preset) => (
          <button
            key={preset.code}
            disabled={busy}
            onClick={() => {
              haptic.tap();
              onPick(preset.code);
            }}
            className="rounded-[9px] border border-edge bg-white/[0.03] px-3.5 py-2.5 text-[13px] text-ink
                       transition-all duration-200 ease-slab active:scale-[0.97] active:bg-white/[0.07]
                       disabled:opacity-40"
          >
            {preset.label}
          </button>
        ))}
      </div>
      <p className="mt-3 font-mono text-[11px] text-muted">
        {active ? `напомню ${whenText(active, Date.now() / 1000)}` : "напоминания нет"}
      </p>
    </div>
  );
}
