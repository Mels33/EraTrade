import { useCallback, useEffect, useState } from "react";
import {
  Account,
  ApiError,
  AuditEntry,
  CommandResult,
  Fill,
  Order,
  Status,
  command,
  get,
  getToken,
  setToken,
} from "./api";

// --- утилиты ---

function usePoll<T>(path: string, intervalMs: number, enabled: boolean) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setData(await get<T>(path));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [path]);
  useEffect(() => {
    if (!enabled) return;
    load();
    const id = setInterval(load, intervalMs);
    return () => clearInterval(id);
  }, [load, intervalMs, enabled]);
  return { data, error, reload: load };
}

const num = (v: string | null | undefined) => (v == null ? NaN : Number(v));
const fmt = (v: string | null | undefined, digits = 2) => {
  const n = num(v);
  return Number.isFinite(n) ? n.toLocaleString("ru-RU", { maximumFractionDigits: digits }) : "—";
};
const time = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("ru-RU", { hour12: false }) : "—";
const age = (seconds: number | null | undefined) => {
  if (seconds == null) return "—";
  if (seconds < 90) return `${Math.round(seconds)} с`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} мин`;
  return `${(seconds / 3600).toFixed(1)} ч`;
};

function Card({ title, children, wide }: { title: string; children: React.ReactNode; wide?: boolean }) {
  return (
    <section className={`card${wide ? " wide" : ""}`}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function Dot({ tone }: { tone: "ok" | "warn" | "bad" | "idle" }) {
  return <span className={`dot ${tone}`} aria-hidden />;
}

// --- вход ---

function Login({ onLogin, error }: { onLogin: (t: string) => void; error: string | null }) {
  const [value, setValue] = useState("");
  return (
    <main className="login">
      <h1>EraTrade</h1>
      <p>Токен оператора (API_TOKEN). Хранится только в этой вкладке.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) onLogin(value.trim());
        }}
      >
        <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
        <button type="submit">Войти</button>
      </form>
      {error && <p className="error">{error}</p>}
    </main>
  );
}

// --- управление ---

interface CommandSpec {
  id: string;
  label: string;
  path: string;
  confirm?: string; // слово, которое нужно ввести
  danger: boolean;
  needsEngine: boolean;
  help: string;
}

const COMMANDS: CommandSpec[] = [
  { id: "pause", label: "Pause", path: "/api/bot/pause", danger: false, needsEngine: false,
    help: "Запретить новые входы, LIVE_ENABLED → LIVE_PAUSED. Закрытие и стопы остаются." },
  { id: "resume", label: "Снять запрет входов", path: "/api/bot/resume", danger: false, needsEngine: false,
    help: "Только после разбора причины запрета. Режим не меняется." },
  { id: "flatten", label: "Flatten", path: "/api/bot/flatten", confirm: "FLATTEN", danger: true, needsEngine: true,
    help: "Закрыть позицию reduceOnly-ордером и снять стопы." },
  { id: "cancel", label: "Cancel all", path: "/api/bot/cancel-all", confirm: "CANCEL_ALL", danger: true, needsEngine: true,
    help: "Отменить ВСЕ открытые ордера, включая защитные стопы." },
  { id: "kill", label: "Kill switch", path: "/api/bot/kill-switch", confirm: "KILL", danger: true, needsEngine: true,
    help: "Пауза → закрыть позицию → отменить ордера → режим DISABLED." },
];

function CommandDialog({ spec, onClose, onDone }: { spec: CommandSpec; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CommandResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [key] = useState(() => crypto.randomUUID()); // повтор того же запроса не выполнит команду дважды

  const ready = reason.trim().length >= 3 && (!spec.confirm || typed === spec.confirm) && !busy;
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = { reason: reason.trim() };
      if (spec.confirm) body.confirm = typed;
      setResult(await command<CommandResult>(spec.path, body, key));
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay" role="dialog" aria-modal>
      <div className="dialog">
        <h3 className={spec.danger ? "danger-text" : ""}>{spec.label}</h3>
        <p>{spec.help}</p>
        {!result && (
          <>
            <label>
              Причина (попадёт в аудит)
              <input value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
            </label>
            {spec.confirm && (
              <label>
                Для подтверждения введите <code>{spec.confirm}</code>
                <input value={typed} onChange={(e) => setTyped(e.target.value)} />
              </label>
            )}
          </>
        )}
        {error && <p className="error">{error}</p>}
        {result && (
          <ul className="steps">
            {result.steps.map((s, i) => <li key={i}>{s}</li>)}
            {result.warnings?.map((w, i) => <li key={`w${i}`} className="error">{w}</li>)}
          </ul>
        )}
        <div className="actions">
          <button onClick={onClose}>{result ? "Закрыть" : "Отмена"}</button>
          {!result && (
            <button className={spec.danger ? "danger" : "primary"} disabled={!ready} onClick={submit}>
              {busy ? "Выполняется…" : "Выполнить"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// --- панель ---

function Banner({ status }: { status: Status }) {
  return (
    <header className={`banner ${status.network}`}>
      <div className="network">{status.network.toUpperCase()}</div>
      <div className="mode">
        <span>{status.coin} {status.interval}</span>
        <strong>{status.mode}</strong>
      </div>
      <div className="entries">
        {status.entries_blocked ? (
          <><Dot tone="bad" /> Входы запрещены: {status.block_reason}</>
        ) : (
          <><Dot tone="ok" /> Запрета входов нет</>
        )}
        <small>изменено {time(status.mode_updated_at)} · {status.mode_updated_by}</small>
      </div>
    </header>
  );
}

function Health({ status }: { status: Status }) {
  const rec = status.reconciliation;
  const recTone = !rec ? "idle" : rec.status === "ok" ? "ok" : rec.status === "warnings" ? "warn" : "bad";
  return (
    <Card title="Состояние">
      <table>
        <tbody>
          {status.components.map((c) => (
            <tr key={c.component}>
              <td><Dot tone={c.last_beat_age_s < 120 ? "ok" : "bad"} />{c.component}</td>
              <td>heartbeat {age(c.last_beat_age_s)} назад</td>
              <td>{String(c.details.ws_state ?? c.details.status ?? "")}</td>
            </tr>
          ))}
          <tr>
            <td><Dot tone={(status.data.last_closed_age_min ?? 999) < 70 ? "ok" : "bad"} />свечи</td>
            <td>последняя закрытая {time(status.data.last_closed_candle)}</td>
            <td>{status.data.last_closed_age_min ?? "—"} мин</td>
          </tr>
          <tr>
            <td><Dot tone={(status.data.context_age_s ?? 999) < 180 ? "ok" : "warn"} />funding/OI</td>
            <td>funding {status.data.funding ?? "—"}, OI {fmt(status.data.open_interest, 0)}</td>
            <td>{age(status.data.context_age_s)}</td>
          </tr>
          <tr>
            <td><Dot tone={recTone} />сверка</td>
            <td>{rec ? `#${rec.run_id} ${rec.status} (${rec.trigger})` : "ещё не запускалась"}</td>
            <td>{rec ? time(rec.finished_at) : ""}</td>
          </tr>
        </tbody>
      </table>
      {rec && rec.differences.filter((d) => !d.repaired || d.severity !== "info").length > 0 && (
        <ul className="diffs">
          {rec.differences.filter((d) => !d.repaired || d.severity !== "info").map((d, i) => (
            <li key={i} className={d.severity}>{d.message}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function LimitBar({ label, pnl, limit }: { label: string; pnl: string; limit: string }) {
  const loss = Math.max(0, -num(pnl));
  const share = Math.min(100, (loss / Math.max(num(limit), 1e-9)) * 100);
  const tone = share >= 100 ? "bad" : share >= 60 ? "warn" : "ok";
  return (
    <div className="limit">
      <div className="limit-head">
        <span>{label}</span>
        <span className={num(pnl) < 0 ? "neg" : "pos"}>{fmt(pnl, 4)} USDC</span>
      </div>
      <div className="bar"><div className={`fill ${tone}`} style={{ width: `${share}%` }} /></div>
      <small>лимит убытка {fmt(limit)} USDC</small>
    </div>
  );
}

function Risk({ account }: { account: Account }) {
  const r = account.risk;
  return (
    <Card title="Риск">
      <div className="kpi">
        <div><small>Equity</small><strong>{fmt(r.equity)} USDC</strong></div>
        <div><small>Сделок сегодня</small><strong>{r.trades_today} / {r.max_trades_per_day}</strong></div>
      </div>
      <LimitBar label="День" pnl={r.daily_pnl} limit={r.daily_limit} />
      <LimitBar label="Неделя" pnl={r.weekly_pnl} limit={r.weekly_limit} />
      <small>комиссии сегодня {fmt(r.fees_today, 4)} · funding {fmt(r.funding_today, 4)}</small>
      {r.blocks.map((b, i) => <p key={i} className="error">{b}</p>)}
    </Card>
  );
}

function Position({ account }: { account: Account }) {
  const p = account.position;
  return (
    <Card title="Позиция">
      {p ? (
        <div className="kpi">
          <div><small>Размер</small><strong className={num(p.size) > 0 ? "pos" : "neg"}>{p.size}</strong></div>
          <div><small>Вход</small><strong>{fmt(p.entry_price, 1)}</strong></div>
          <div><small>uPnL</small><strong className={num(p.unrealized_pnl) < 0 ? "neg" : "pos"}>{fmt(p.unrealized_pnl, 4)}</strong></div>
          <div><small>Ликвидация</small><strong>{fmt(p.liquidation_price, 0)}</strong></div>
          <div><small>Плечо</small><strong>{p.leverage}</strong></div>
        </div>
      ) : (
        <p className="muted">Позиции нет</p>
      )}
      <h3>Открытые ордера на бирже</h3>
      {account.open_orders.length === 0 ? (
        <p className={p ? "error" : "muted"}>{p ? "Нет стопа!" : "Нет"}</p>
      ) : (
        <table>
          <tbody>
            {account.open_orders.map((o) => (
              <tr key={o.oid}>
                <td>{o.order_type}{o.reduce_only ? " · reduceOnly" : ""}</td>
                <td>{o.side} {o.size}</td>
                <td>{o.trigger_price ? `trigger ${fmt(o.trigger_price, 1)}` : fmt(o.limit_price, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function Orders({ orders }: { orders: Order[] }) {
  return (
    <Card title="Ордера бота" wide>
      <div className="scroll">
        <table>
          <thead><tr><th>Время</th><th>Тип</th><th>Сторона</th><th>Размер</th><th>Цена</th><th>Статус</th><th>Исполнено</th></tr></thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id} title={o.error ?? o.cloid}>
                <td>{time(o.created_at)}</td>
                <td>{o.purpose}</td>
                <td>{o.side}</td>
                <td>{o.size}</td>
                <td>{o.trigger_price ? `trig ${fmt(o.trigger_price, 1)}` : fmt(o.price, 1)}</td>
                <td className={`status ${o.status.toLowerCase()}`}>{o.status}</td>
                <td>{o.filled_size}{o.avg_fill_price ? ` @ ${fmt(o.avg_fill_price, 1)}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Fills({ fills }: { fills: Fill[] }) {
  return (
    <Card title="Исполнения">
      <div className="scroll">
        <table>
          <tbody>
            {fills.map((f) => (
              <tr key={f.tid}>
                <td>{time(f.time)}</td>
                <td>{f.direction}{f.bot_order ? "" : " (не бот)"}</td>
                <td>{f.size} @ {fmt(f.price, 1)}</td>
                <td className={num(f.closed_pnl) < 0 ? "neg" : ""}>{fmt(f.closed_pnl, 4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Audit({ entries }: { entries: AuditEntry[] }) {
  return (
    <Card title="Аудит">
      <div className="scroll">
        <table>
          <tbody>
            {entries.map((a) => (
              <tr key={a.id}>
                <td>{time(a.occurred_at)}</td>
                <td>{a.actor}</td>
                <td>{a.action}{a.reason ? ` — ${a.reason}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export default function App() {
  const [token, setTokenState] = useState(getToken());
  const [dialog, setDialog] = useState<CommandSpec | null>(null);
  const enabled = Boolean(token);
  const status = usePoll<Status>("/api/status", 5000, enabled);
  const account = usePoll<Account>("/api/account", 10000, enabled);
  const orders = usePoll<Order[]>("/api/orders?limit=20", 15000, enabled);
  const fills = usePoll<Fill[]>("/api/fills?limit=20", 15000, enabled);
  const audit = usePoll<AuditEntry[]>("/api/audit?limit=20", 15000, enabled);

  const unauthorized = status.error?.includes("токен");
  if (!token || unauthorized) {
    return (
      <Login
        error={unauthorized ? "Токен не принят" : null}
        onLogin={(t) => {
          setToken(t);
          setTokenState(t);
        }}
      />
    );
  }

  const refreshAll = () => {
    status.reload();
    account.reload();
    orders.reload();
    audit.reload();
  };

  return (
    <div className="app">
      {status.data ? <Banner status={status.data} /> : <header className="banner">Загрузка…</header>}
      {status.error && <p className="error">API: {status.error}</p>}
      <nav className="controls">
        {COMMANDS.map((c) => (
          <button
            key={c.id}
            className={c.danger ? "danger" : ""}
            disabled={c.needsEngine && status.data?.trading_commands_available === false}
            title={c.needsEngine && status.data?.trading_commands_available === false ? "ключ API-кошелька не подключён к API" : c.help}
            onClick={() => setDialog(c)}
          >
            {c.label}
          </button>
        ))}
        <button className="ghost" onClick={() => { setToken(null); setTokenState(null); }}>Выйти</button>
      </nav>
      <div className="grid">
        {status.data && <Health status={status.data} />}
        {account.data ? <Risk account={account.data} /> : <Card title="Риск"><p className="muted">{account.error ?? "Загрузка…"}</p></Card>}
        {account.data && <Position account={account.data} />}
        {orders.data && <Orders orders={orders.data} />}
        {fills.data && <Fills fills={fills.data} />}
        {audit.data && <Audit entries={audit.data} />}
      </div>
      {dialog && <CommandDialog spec={dialog} onClose={() => setDialog(null)} onDone={refreshAll} />}
    </div>
  );
}
