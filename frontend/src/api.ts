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

export interface Difference {
  kind: string;
  severity: "info" | "warning" | "critical";
  message: string;
  repaired: boolean;
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
  reconciliation: {
    run_id: number;
    status: string;
    trigger: string;
    finished_at: string | null;
    differences: Difference[];
  } | null;
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

export interface Signal {
  id: number;
  decision_time: string;
  model_version: string;
  model_role: "active" | "shadow";
  decision: "BUY" | "SELL" | "HOLD" | "EXIT";
  reason: string;
  probabilities: Record<string, number> | null;
  expected_return_bps: number | null;
  position_side: string | null;
  outcome_return: number | null;
  outcome_label: string | null;
}

export interface ModelVersion {
  version: string;
  status: "candidate" | "shadow" | "active" | "retired";
  created_at: string;
  activated_at: string | null;
  test: {
    log_loss: number | null;
    baseline_log_loss: number | null;
    ece: number | null;
    policy: { buy_signals: number; sell_signals: number; precision: number | null; mean_net_return: number | null } | null;
  };
}

export interface ShadowModel {
  version: string;
  status: string;
  signals: number;
  coverage: number | null;
  decisions: Record<string, number>;
  feature_or_model_errors: Record<string, number>;
  probabilities: { mean: Record<string, number> | null; max_directional_p50: number | null };
  calibration: { resolved: number; log_loss?: number; baseline_log_loss?: number; ece?: number; enough?: boolean };
  entries: { resolved: number; precision: number | null; mean_net_return: number | null };
  latency_s: { p50: number | null; p95: number | null };
  market: { spread_bps_mean: number | null; entry_slippage_bps_mean: number | null };
  risk_refusals: { count: number; reasons: Record<string, number> };
  drift: { feature: string; shift_sigma?: number; out_of_bounds?: number }[];
}

export interface ShadowReport {
  snapshots: number;
  min_outcomes_for_metrics: number;
  drift_sigma_warn: number;
  models: ShadowModel[];
}

export interface Readiness {
  summary: "ok" | "warn" | "fail";
  checks: { name: string; status: "ok" | "warn" | "fail"; detail: string }[];
}

export interface Trade {
  side: string;
  opened_at: string;
  closed_at: string | null;
  size: string;
  notional: string;
  entry_price: string;
  exit_price: string | null;
  fees: string;
  funding: string;
  net: string;
  exit_kind: string | null;
  bot_entry: boolean;
  checks: string[];
  notes: string[];
  entry_slippage_vs_preview_bps: number | null;
}
