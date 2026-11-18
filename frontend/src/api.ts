// Клиент API панели. Токен оператора хранится только в sessionStorage этой вкладки.

const TOKEN_KEY = "eratrade.token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* приватный режим — токен просто не запомнится */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = body?.detail;
    throw new ApiError(response.status, typeof detail === "string" ? detail : `HTTP ${response.status}`);
  }
  return body as T;
}

export const get = <T>(path: string) => request<T>(path);

export function command<T>(path: string, body: Record<string, unknown>, idempotencyKey: string) {
  return request<T>(path, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": idempotencyKey },
  });
}

// --- типы ответов ---

export interface Component {
  component: string;
  last_beat_age_s: number;
  details: Record<string, unknown>;
}


export interface Status {
  network: "mainnet" | "testnet";
  coin: string;
  interval: string;
  mode: string;
  entries_blocked: boolean;
  block_reason: string | null;
  mode_updated_at: string | null;
  mode_updated_by: string | null;
  trading_commands_available: boolean;
  components: Component[];
  data: {
    last_closed_candle: string | null;
    last_closed_age_min: number | null;
    context_age_s: number | null;
    funding: string | null;
    open_interest: string | null;
    mark_price: string | null;
  };
}

export interface Account {
  exchange_time: string;
  position: {
    size: string;
    entry_price: string | null;
    unrealized_pnl: string;
    liquidation_price: string | null;
    margin_used: string;
    leverage: string;
  } | null;
  open_orders: {
    oid: number;
    side: string;
    size: string;
    limit_price: string;
    trigger_price: string | null;
    order_type: string;
    reduce_only: boolean;
  }[];
  risk: {
    equity: string;
    spot_usdc: string;
    perp_account_value: string;
    unrealized_pnl: string;
    daily_pnl: string;
    daily_limit: string;
    weekly_pnl: string;
    weekly_limit: string;
    fees_today: string;
    funding_today: string;
    trades_today: number;
    max_trades_per_day: number;
    blocks: string[];
  };
}

export interface Order {
  id: string;
  cloid: string;
  purpose: string;
  side: string;
  size: string;
  price: string;
  trigger_price: string | null;
  status: string;
  filled_size: string;
  avg_fill_price: string | null;
  error: string | null;
  created_at: string;
}

export interface Fill {
  tid: number;
  time: string;
  side: string;
  size: string;
  price: string;
  fee: string;
  closed_pnl: string;
  direction: string;
  bot_order: boolean;
}

export interface AuditEntry {
  id: number;
  occurred_at: string;
  actor: string;
  action: string;
  reason: string | null;
}

export interface CommandResult {
  steps: string[];
  warnings?: string[];
}





export interface Candle {
  open_time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
}



export interface MmQuote {
  price: string;
  size: string;
  pending: boolean;
}

export interface MmCoin {
  mid: string | null;
  spread_bps: number | null;
  bid: MmQuote | null;
  ask: MmQuote | null;
  position: string;
  position_usd: string | null;
  unrealized: string;
  flow: number | null;
  reasons: string[];
  book_age_s: number | null;
  retiring?: boolean;
}

export interface MmOverview {
  curve: { t: string; pnl: string }[];
  config: { coins: string[]; order_usd: string; max_inventory_usd: string; daily_loss_usd: string };
  live: {
    last_beat_age_s: number;
    started_at: string;
    state: string;
    quoting: boolean;
    pnl_today: string;
    realized_today: string;
    daily_loss_limit: string;
    account_value: string | null;
    spot_usdc: string | null;
    actions_last_min: number;
    actions_limit: number;
    requests_remaining: number | null;
    ws: boolean;
    dms_error: string | null;
    auto_select?: boolean;
    selection?: {
      at?: string;
      next_at?: string;
      target?: string[];
      candidates?: { coin: string; spread_bps: number; tick_bps: number; volume_usd: number; score: number }[];
    };
    coins: Record<string, MmCoin>;
  } | null;
  fills: {
    time: string;
    coin: string;
    side: string;
    price: string;
    size: string;
    notional: string;
    fee: string;
    closed_pnl: string;
    crossed: boolean;
  }[];
  daily: { day: string; coin: string; fills: number; volume: string; fees: string; net: string; taker_fills: number }[];
}
