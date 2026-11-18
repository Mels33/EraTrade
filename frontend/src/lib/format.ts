// Форматирование чисел и времени для панели.

export const num = (v: string | number | null | undefined): number =>
  v == null || v === "" ? NaN : Number(v);

export function money(v: string | number | null | undefined, digits = 2): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function price(v: string | number | null | undefined): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("ru-RU", { maximumFractionDigits: n >= 1000 ? 0 : 2 });
}

export function signed(v: string | number | null | undefined, digits = 2): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  const s = money(Math.abs(n), digits);
  return n > 0 ? `+${s}` : n < 0 ? `−${s}` : s;
}

export function percent(x: number | null | undefined, digits = 0): string {
  if (x == null || !Number.isFinite(x)) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}

export function signedPercent(x: number | null | undefined, digits = 2): string {
  if (x == null || !Number.isFinite(x)) return "—";
  const s = `${Math.abs(x * 100).toFixed(digits)}%`;
  return x > 0 ? `+${s}` : x < 0 ? `−${s}` : s;
}

export const tone = (v: string | number | null | undefined): "up" | "down" | "flat" => {
  const n = num(v);
  return !Number.isFinite(n) || n === 0 ? "flat" : n > 0 ? "up" : "down";
};

const pad = (n: number) => String(n).padStart(2, "0");

export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const yesterday = new Date(today.getTime() - 86_400_000).toDateString() === d.toDateString();
  const day = sameDay ? "сегодня" : yesterday ? "вчера" : `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
  return `${day}, ${clock(iso)}`;
}

export function ago(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} с назад`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин назад`;
  if (seconds < 86_400) return `${(seconds / 3600).toFixed(1).replace(".0", "")} ч назад`;
  return `${Math.round(seconds / 86_400)} дн назад`;
}

export function agoIso(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  return ago((now - new Date(iso).getTime()) / 1000);
}

/** Длительность: «1 ч 12 мин», «5 мин», «40 с». */
export function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)} с`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m} мин`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} ч ${m % 60} мин`;
  return `${Math.floor(h / 24)} дн ${h % 24} ч`;
}

/** Цена с нужной точностью: мелкие монеты — до 6 знаков. */
export function px(v: string | number | null | undefined): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  const digits = n >= 1000 ? 1 : n >= 10 ? 3 : n >= 1 ? 4 : 6;
  return n.toLocaleString("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}
