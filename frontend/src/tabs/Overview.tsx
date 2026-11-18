import type { Account, Candle, ModelVersion, Signal, Status } from "../api";
import { PriceChart, ProbabilityBars } from "../components/charts";
import { Card, Dot, Empty, ErrorText, Loading, Meter, Pill, Stat } from "../components/ui";
import {
  countdown,
  dateTime,
  money,
  nextDecision,
  num,
  price,
  signed,
  signedPercent,
  tone,
} from "../lib/format";
import { DECISION, headline, healthIssues, modelName, reasonText } from "../lib/labels";
import { useNow, usePoll } from "../lib/usePoll";

export function Overview({ status, models }: { status: Status; models: ModelVersion[] | null }) {
  const account = usePoll<Account>("/api/account", 10_000);
  const candles = usePoll<Candle[]>("/api/candles?hours=48", 60_000);
  const signals = usePoll<{ items: Signal[] }>("/api/signals?limit=4", 30_000);

  return (
    <div className="page">
      <Hero status={status} models={models} />
      <div className="row row-2-1">
        <PriceCard candles={candles.data} error={candles.error} mark={status.data.mark_price} />
        <BalanceCard account={account.data} error={account.error} />
      </div>
      <div className="row row-1-1">
        <SignalCard signals={signals.data?.items ?? null} />
        <PositionCard account={account.data} />
      </div>
    </div>
  );
}

function Hero({ status, models }: { status: Status; models: ModelVersion[] | null }) {
  const now = useNow();
  const h = headline(status, models);
  const issues = healthIssues(status);
  return (
    <section className={`hero ${h.tone}`}>
      <div className="hero-main">
        <span className={`hero-dot ${h.tone}`} />
        <div>
          <h1>{h.title}</h1>
          <p>{h.subtitle}</p>
        </div>
      </div>
      <div className="hero-side">
        <div className="hero-health">
          {issues.length === 0 ? (
            <>
              <Dot tone="ok" /> Все системы работают
            </>
          ) : (
            <>
              <Dot tone="bad" /> {issues.join(" · ")}
            </>
          )}
        </div>
        <div className="hero-timer">
          <span>Следующее решение через</span>
          <strong>{countdown(nextDecision(now) - now)}</strong>
        </div>
      </div>
    </section>
  );
}

function PriceCard({ candles, error, mark }: { candles: Candle[] | null; error: string | null; mark: string | null }) {
  const last = candles?.length ? num(candles[candles.length - 1].close) : NaN;
  const first = candles?.length ? num(candles[0].close) : NaN;
  // то же изменение, что задаёт цвет графика: за весь видимый период
  const change = Number.isFinite(last) && Number.isFinite(first) && candles!.length > 1 ? last / first - 1 : null;
  return (
    <Card title="Bitcoin, 48 часов" hint="Цена закрытия часовых свечей Hyperliquid">
      <div className="price-head">
        <span className="price-big">{price(mark ?? (Number.isFinite(last) ? last : null))}</span>
        {change !== null && <Pill tone={tone(change)}>{signedPercent(change)} за 48 ч</Pill>}
      </div>
      {error ? <ErrorText>{error}</ErrorText> : candles ? <PriceChart candles={candles} /> : <Loading />}
    </Card>
  );
}

function BalanceCard({ account, error }: { account: Account | null; error: string | null }) {
  if (!account) return <Card title="Баланс и лимиты">{error ? <ErrorText>{error}</ErrorText> : <Loading />}</Card>;
  const r = account.risk;
  return (
    <Card title="Баланс и лимиты" hint="Убыток считается вместе с комиссиями, funding и открытой позицией. При достижении лимита новые сделки блокируются.">
      <Stat label="Баланс" value={`${money(r.equity)} USDC`} />
      <Meter
        label="Убыток за день"
        value={Math.max(0, -num(r.daily_pnl))}
        max={num(r.daily_limit)}
        right={<span className={tone(r.daily_pnl)}>{signed(r.daily_pnl, 2)} USDC</span>}
        caption={`лимит ${money(r.daily_limit)} USDC`}
      />
      <Meter
        label="Убыток за неделю"
        value={Math.max(0, -num(r.weekly_pnl))}
        max={num(r.weekly_limit)}
        right={<span className={tone(r.weekly_pnl)}>{signed(r.weekly_pnl, 2)} USDC</span>}
        caption={`лимит ${money(r.weekly_limit)} USDC`}
      />
      <Meter
        label="Сделок сегодня"
        value={r.trades_today}
        max={r.max_trades_per_day}
        right={`${r.trades_today} из ${r.max_trades_per_day}`}
      />
      {r.blocks.map((b, i) => (
        <ErrorText key={i}>{b}</ErrorText>
      ))}
    </Card>
  );
}

function SignalCard({ signals }: { signals: Signal[] | null }) {
  if (!signals) return <Card title="Последний сигнал модели"><Loading /></Card>;
  if (signals.length === 0)
    return (
      <Card title="Последний сигнал модели">
        <Empty>Сигналов пока нет — они появляются раз в час после закрытия свечи.</Empty>
      </Card>
    );
  const latestTime = signals[0].decision_time;
  const latest = signals.filter((s) => s.decision_time === latestTime);
  const s = latest.find((x) => x.model_role === "active") ?? latest[0];
  const d = DECISION[s.decision] ?? { label: s.decision, tone: "flat" as const };
  return (
    <Card
      title="Последний сигнал модели"
      hint="Раз в час модель оценивает, куда пойдёт цена за 4 часа. Сделка открывается, только если уверенность и ожидаемая выгода выше порога."
    >
      <div className="signal-head">
        <Pill tone={d.tone === "warn" ? "warn" : d.tone}>{d.label}</Pill>
        <span className="muted">
          {dateTime(s.decision_time)} · {s.model_role === "active" ? "торгующая модель" : "наблюдение, без сделок"}
        </span>
      </div>
      {s.probabilities ? <ProbabilityBars probs={s.probabilities} /> : null}
      <p className="signal-reason" title={s.reason}>
        {reasonText(s.reason)}
      </p>
      <p className="muted small">{modelName(s.model_version)}</p>
    </Card>
  );
}

function PositionCard({ account }: { account: Account | null }) {
  if (!account) return <Card title="Позиция"><Loading /></Card>;
  const p = account.position;
  const stop = account.open_orders.find((o) => o.trigger_price && o.reduce_only);
  if (!p)
    return (
      <Card title="Позиция">
        <div className="no-position">
          <span className="no-position-icon">○</span>
          <div>
            <strong>Позиции нет</strong>
            <p className="muted">Бот не держит BTC. Деньги на балансе.</p>
          </div>
        </div>
        {account.open_orders.length > 0 && (
          <ErrorText>На бирже висят ордера без позиции: {account.open_orders.length}</ErrorText>
        )}
      </Card>
    );
  const long = num(p.size) > 0;
  return (
    <Card title="Позиция">
      <div className="stats-grid">
        <Stat label="Направление" value={long ? "Long (рост)" : "Short (падение)"} tone={long ? "up" : "down"} />
        <Stat label="Размер" value={`${Math.abs(num(p.size))} BTC`} />
        <Stat label="Цена входа" value={price(p.entry_price)} />
        <Stat label="Прибыль сейчас" value={`${signed(p.unrealized_pnl, 4)} USDC`} tone={tone(p.unrealized_pnl)} />
      </div>
      {stop ? (
        <p className="ok-text">✓ Защитный стоп на {price(stop.trigger_price)}</p>
      ) : (
        <ErrorText>⚠ Позиция без защитного стопа!</ErrorText>
      )}
    </Card>
  );
}
