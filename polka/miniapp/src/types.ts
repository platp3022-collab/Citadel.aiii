export type Shelf = {
  key: string;
  title: string;
  subtitle: string;
  count: number;
  sort: number;
};

export type Reminder = {
  id: string;
  mode: "once" | "every";
  intervalMin: number;
  nextFireAt: number;
  escalation: number;
};

export type Thought = {
  id: string;
  title: string;
  summary: string;
  raw: string;
  shelf: string;
  kind: string;
  importance: number;
  status: string;
  tags: string[];
  claudeWorthy: boolean;
  hasPrompt: boolean;
  createdAt: number;
  reminders: Reminder[];
};

export type Preset = { code: string; label: string };

export type State = {
  now: number;
  shelves: Shelf[];
  thoughts: Thought[];
  presets: Preset[];
};
