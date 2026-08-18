/** Мост к Telegram WebApp: тактильная отдача, кнопка назад, initData для API. */

type HapticStyle = "light" | "medium" | "heavy" | "rigid" | "soft";
type HapticKind = "error" | "success" | "warning";

type WebApp = {
  initData: string;
  ready(): void;
  expand(): void;
  close(): void;
  setHeaderColor(color: string): void;
  setBackgroundColor(color: string): void;
  disableVerticalSwipes?(): void;
  BackButton: { show(): void; hide(): void; onClick(cb: () => void): void; offClick(cb: () => void): void };
  HapticFeedback: {
    impactOccurred(style: HapticStyle): void;
    notificationOccurred(kind: HapticKind): void;
    selectionChanged(): void;
  };
};

const app: WebApp | undefined = (window as unknown as { Telegram?: { WebApp: WebApp } })
  .Telegram?.WebApp;

export const inTelegram = Boolean(app?.initData);

export function boot(): void {
  if (!app) return;
  app.ready();
  app.expand();
  app.setHeaderColor("#0b0b0c");
  app.setBackgroundColor("#0b0b0c");
  // Иначе свайп вниз по списку закрывает приложение вместо прокрутки.
  app.disableVerticalSwipes?.();
}

export const initData = app?.initData ?? "";

/** Тактильная отдача. Вне Telegram молча ничего не делает. */
export const haptic = {
  tap: () => app?.HapticFeedback.impactOccurred("light"),
  press: () => app?.HapticFeedback.impactOccurred("medium"),
  slam: () => app?.HapticFeedback.impactOccurred("heavy"),
  pick: () => app?.HapticFeedback.selectionChanged(),
  done: () => app?.HapticFeedback.notificationOccurred("success"),
  alarm: () => app?.HapticFeedback.notificationOccurred("error"),
};

export function backButton(visible: boolean, handler: () => void): () => void {
  if (!app) return () => {};
  const { BackButton } = app;
  BackButton.onClick(handler);
  if (visible) BackButton.show();
  else BackButton.hide();
  return () => {
    BackButton.offClick(handler);
    BackButton.hide();
  };
}
