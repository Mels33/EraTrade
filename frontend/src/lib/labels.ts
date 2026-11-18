// Человеческие тексты вместо внутренних кодов.

export type Tone = "ok" | "warn" | "bad" | "idle" | "info";

const COMPONENT_LIMIT_S: Record<string, number> = {
  mm: 60,
};

export const COMPONENTS: Record<string, { name: string; hint: string }> = {
  mm: { name: "Маркет-мейкер", hint: "Ставит и переставляет заявки на бирже" },
};

export function componentState(component: string, ageS: number): { tone: Tone; text: string } {
  const limit = COMPONENT_LIMIT_S[component] ?? 120;
  return ageS < limit ? { tone: "ok", text: "работает" } : { tone: "bad", text: "не отвечает" };
}

export const MODE_LABEL: Record<string, string> = {
  DISABLED: "Выключен",
  TESTNET: "Тестовая сеть",
  SHADOW: "Наблюдение",
  LIVE_PAUSED: "Пауза",
  LIVE_ENABLED: "Торгует",
};

export function modeTone(mode: string, blocked: boolean): Tone {
  if (blocked) return "bad";
  return mode === "LIVE_ENABLED" ? "ok" : mode === "LIVE_PAUSED" ? "warn" : "idle";
}

const AUDIT: Record<string, string> = {
  "bot_state.created": "Создано состояние бота",
  "bot_state.mode_changed": "Изменён режим",
  "bot_state.entries_blocked": "Запрещены новые входы",
  "bot_state.entries_unblocked": "Снят запрет входов",
  "emergency.cancel_all": "Отменены все ордера",
  "emergency.full_kill": "Аварийная остановка",
  "leverage.updated": "Изменено плечо",
  "position.baseline_accepted": "Позиция принята как исходная",
  "model.registered": "Модель зарегистрирована (прежний подход)",
  "model.shadow": "Модель в наблюдении (прежний подход)",
  "model.activated": "Модель активирована (прежний подход)",
  "model.retired": "Модель в архиве (прежний подход)",
};

export function auditText(action: string): string {
  return AUDIT[action] ?? action;
}

export function actorText(actor: string): string {
  const [kind, name] = actor.split(":");
  if (kind === "operator") return `оператор ${name ?? ""}`.trim();
  if (kind === "system") return name === "startup" ? "система (старт)" : `система (${name ?? ""})`;
  if (kind === "config") return "конфигурация";
  return actor;
}
