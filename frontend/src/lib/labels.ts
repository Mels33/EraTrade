// Человеческие тексты вместо внутренних кодов.

import type { ModelVersion, Status } from "../api";

export type Tone = "ok" | "warn" | "bad" | "idle" | "info";

// --- модели ---

export const MODEL_STATUS: Record<string, { label: string; tone: Tone }> = {
  candidate: { label: "Кандидат", tone: "idle" },
  shadow: { label: "Наблюдение", tone: "info" },
  active: { label: "Торгует", tone: "ok" },
  retired: { label: "В архиве", tone: "idle" },
};

/** cb-v1-202611171414-e61a94d5 → «модель от 17.11, 14:14» */
export function modelName(version: string): string {
  const m = version.match(/-(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})-/);
  return m ? `модель от ${m[3]}.${m[2]}, ${m[4]}:${m[5]}` : version;
}

// --- решения ---

export const DECISION: Record<string, { label: string; tone: "up" | "down" | "flat" | "warn" }> = {
  BUY: { label: "Покупка", tone: "up" },
  SELL: { label: "Продажа", tone: "down" },
  HOLD: { label: "Ждём", tone: "flat" },
  EXIT: { label: "Закрыть позицию", tone: "warn" },
};

/** Причина решения политики простыми словами (оригинал — во всплывающей подсказке). */
export function reasonText(reason: string): string {
  const r = reason.toLowerCase();
  if (r.startsWith("hold из-за ошибки")) {
    if (r.includes("опаздывает")) return "Решение опоздало — по старым данным не торгуем";
    if (r.includes("признаки") || r.includes("свеч") || r.includes("funding"))
      return "Не хватает свежих данных — сделка не рассматривалась";
    if (r.includes("вне границ")) return "Рынок вне диапазона, на котором училась модель";
    return "Ошибка данных или модели — на всякий случай ждём";
  }
  if (r.startsWith("выход по времени")) return "Позиция держится 8 часов — закрываем по времени";
  if (r.includes("cooldown")) return "Пауза после выхода — ждём следующую свечу";
  if (r.includes("не увеличиваем")) return "Позиция уже открыта — не добавляем";
  if (r.includes("противоположный сигнал")) return "Сигнал против позиции — закрываем без разворота";
  if (r.includes("держим позицию")) return "Держим позицию";
  if (r.includes("запрещён политикой")) return "Это направление запрещено настройками";
  if (r.includes("нет уверенного направления")) return "Модель не уверена в направлении движения";
  if (r.startsWith("p(up)=")) return "Модель ждёт рост — преимущество выше издержек";
  if (r.startsWith("p(down)=")) return "Модель ждёт падение — преимущество выше издержек";
  return reason;
}

// --- режим и общий статус ---

const COMPONENT_LIMIT_S: Record<string, number> = {
  collector: 60,
  reconciler: 120,
  shadow: 120,
  trader: 120,
};

export const COMPONENTS: Record<string, { name: string; hint: string }> = {
  collector: { name: "Сбор данных", hint: "Свечи и funding с биржи в реальном времени" },
  reconciler: {
    name: "Сверка с биржей",
    hint: "Раз в минуту сравнивает позицию, ордера и сделки в базе с биржей",
  },
  shadow: { name: "Наблюдение", hint: "Каждый час считает сигнал модели, не торгуя" },
  trader: { name: "Торговый цикл", hint: "Исполняет сигналы. Запускается вручную, когда торгуем" },
};

export function componentState(
  component: string,
  ageS: number,
  mode: string,
): { tone: Tone; text: string } {
  const limit = COMPONENT_LIMIT_S[component] ?? 120;
  if (ageS < limit) return { tone: "ok", text: "работает" };
  if (component === "trader" && mode !== "LIVE_ENABLED")
    return { tone: "idle", text: "не запущен (торговля выключена)" };
  return { tone: "bad", text: "не отвечает" };
}

export function healthIssues(status: Status): string[] {
  const issues: string[] = [];
  for (const c of status.components) {
    const state = componentState(c.component, c.last_beat_age_s, status.mode);
    if (state.tone === "bad") issues.push(`${COMPONENTS[c.component]?.name ?? c.component}: не отвечает`);
  }
  for (const needed of ["collector", "reconciler"]) {
    if (!status.components.some((c) => c.component === needed))
      issues.push(`${COMPONENTS[needed].name}: не запускался`);
  }
  if ((status.data.last_closed_age_min ?? 999) > 70) issues.push("Свечи устарели");
  const rec = status.reconciliation;
  if (rec && rec.status !== "ok") issues.push("Сверка нашла расхождения");
  return issues;
}

export interface Headline {
  tone: Tone;
  title: string;
  subtitle: string;
}

export function headline(status: Status, models: ModelVersion[] | null): Headline {
  const active = models?.find((m) => m.status === "active");
  const shadow = models?.find((m) => m.status === "shadow");
  const modelLine = active
    ? `Торгует ${modelName(active.version)}.`
    : shadow
      ? `Активной модели нет — ${modelName(shadow.version)} в наблюдении и сделок не открывает.`
      : "Моделей нет.";

  if (status.entries_blocked) {
    return {
      tone: "bad",
      title: "Новые сделки запрещены",
      subtitle: `${status.block_reason ?? "причина не указана"}. ${modelLine}`,
    };
  }
  switch (status.mode) {
    case "LIVE_ENABLED":
      return active
        ? { tone: "ok", title: "Бот торгует", subtitle: modelLine }
        : { tone: "warn", title: "Торговля включена, но модели нет", subtitle: modelLine };
    case "LIVE_PAUSED":
      return {
        tone: "warn",
        title: "Бот на паузе",
        subtitle: `Новые сделки не открываются. ${modelLine}`,
      };
    case "SHADOW":
      return { tone: "info", title: "Режим наблюдения", subtitle: `Ордера не отправляются. ${modelLine}` };
    case "TESTNET":
      return { tone: "info", title: "Тестовая сеть", subtitle: modelLine };
    default:
      return { tone: "idle", title: "Бот выключен", subtitle: `Ордера не отправляются. ${modelLine}` };
  }
}

export const MODE_LABEL: Record<string, string> = {
  DISABLED: "Выключен",
  TESTNET: "Тестовая сеть",
  SHADOW: "Наблюдение",
  LIVE_PAUSED: "Пауза",
  LIVE_ENABLED: "Торговля",
};

// --- ордера и сделки ---

export const ORDER_PURPOSE: Record<string, string> = {
  entry: "Вход",
  exit: "Выход",
  stop: "Защитный стоп",
};

export const ORDER_STATUS: Record<string, { label: string; tone: Tone }> = {
  FILLED: { label: "Исполнен", tone: "ok" },
  PARTIALLY_FILLED: { label: "Частично исполнен", tone: "warn" },
  RESTING: { label: "Стоит на бирже", tone: "info" },
  TRIGGERED: { label: "Сработал", tone: "warn" },
  CANCELED: { label: "Отменён", tone: "idle" },
  REJECTED: { label: "Отклонён биржей", tone: "bad" },
  BLOCKED: { label: "Не отправлен", tone: "idle" },
  UNKNOWN: { label: "Уточняется", tone: "warn" },
  NOT_SENT: { label: "Не отправлен", tone: "idle" },
  CREATED: { label: "Создан", tone: "idle" },
  APPROVED: { label: "Одобрен", tone: "idle" },
  SIGNED: { label: "Подписан", tone: "info" },
};

export const SIDE: Record<string, string> = { buy: "Покупка", sell: "Продажа", long: "Long", short: "Short" };

export const FILL_DIRECTION: Record<string, string> = {
  "Open Long": "Открытие long",
  "Close Long": "Закрытие long",
  "Open Short": "Открытие short",
  "Close Short": "Закрытие short",
};

export const EXIT_KIND: Record<string, string> = {
  exit: "по сигналу",
  stop: "по стопу",
  manual: "вручную",
  entry: "—",
};

// --- журнал ---

const AUDIT: Record<string, string> = {
  "bot_state.created": "Создано состояние бота",
  "bot_state.mode_changed": "Изменён режим",
  "bot_state.entries_blocked": "Запрещены новые входы",
  "bot_state.entries_unblocked": "Снят запрет входов",
  "emergency.cancel_all": "Отменены все ордера",
  "emergency.full_kill": "Аварийная остановка",
  "leverage.updated": "Изменено плечо",
  "model.registered": "Зарегистрирована новая модель",
  "model.shadow": "Модель переведена в наблюдение",
  "model.activated": "Модель активирована для торговли",
  "model.rollback": "Откат на прежнюю модель",
  "model.retired": "Модель отправлена в архив",
  "position.baseline_accepted": "Позиция принята как исходная",
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

// --- признаки модели ---

export function featureLabel(name: string): string {
  if (name.startsWith("ret_")) return `изменение цены за ${name.slice(4)} ч`;
  if (name.startsWith("rv_") || name === "atr_14_rel") return "волатильность";
  if (name.startsWith("dist_sma") || name.startsWith("slope_sma")) return "тренд";
  if (name.startsWith("range") || name === "close_pos" || name === "body") return "форма свечей";
  if (name.startsWith("volume")) return "объём торгов";
  if (name.startsWith("trades") || name.startsWith("trade_size")) return "число и размер сделок";
  if (name.startsWith("funding")) return "funding";
  if (name.startsWith("premium")) return "премия к цене оракула";
  if (name.startsWith("hour_")) return "время суток";
  return name;
}

// --- проверки готовности ---

const CHECK_NAMES: Record<string, string> = {
  сеть: "Сеть",
  "ручное подтверждение каждого ордера": "Каждый ордер подтверждается вручную",
  "минимальные лимиты": "Минимальные лимиты риска",
  "уведомления оператору": "Уведомления в Telegram",
  "API-кошелёк": "Торговый ключ (API-кошелёк)",
  режим: "Режим работы",
  "запрет входов": "Нет запрета входов",
  сверка: "Свежая сверка с биржей",
  "компонент collector": "Сбор данных работает",
  "компонент reconciler": "Сверка работает",
  "компонент shadow": "Наблюдение работает",
  "свежесть свечей": "Свежие рыночные данные",
  "активная модель": "Есть торгующая модель",
  "доказательства shadow": "Модель проверена в наблюдении",
  "shadow-модель": "Модель в наблюдении",
  "kill switch": "Аварийная остановка опробована",
  "бэкап БД": "Свежий бэкап базы",
  "проверка восстановления": "Бэкап проверен восстановлением",
};

export function checkName(name: string): string {
  return CHECK_NAMES[name] ?? name;
}
