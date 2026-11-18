import type { Account, Fill, Order, Trade } from "../api";
import { Card, Empty, ErrorText, Loading, Pill, Stat } from "../components/ui";
import { dateTime, money, num, price, signed, tone } from "../lib/format";
import { EXIT_KIND, FILL_DIRECTION, ORDER_PURPOSE, ORDER_STATUS, SIDE } from "../lib/labels";
import { usePoll } from "../lib/usePoll";

export function Trading() {
  const account = usePoll<Account>("/api/account", 10_000);
  const trades = usePoll<Trade[]>("/api/trades?limit=30", 30_000);
  const orders = usePoll<Order[]>("/api/orders?limit=30", 15_000);
  const fills = usePoll<Fill[]>("/api/fills?limit=30", 30_000);

  return (
    <div className="page">
      <div className="row row-1-1">
        <PositionDetails account={account.data} error={account.error} />
        <ExchangeOrders account={account.data} />
      </div>
      <TradesCard trades={trades.data} />
      <div className="row row-1-1">
        <BotOrders orders={orders.data} />
        <Fills fills={fills.data} />
      </div>
    </div>
  );
}

function PositionDetails({ account, error }: { account: Account | null; error: string | null }) {
  if (!account) return <Card title="Позиция">{error ? <ErrorText>{error}</ErrorText> : <Loading />}</Card>;
  const p = account.position;
  if (!p)
    return (
      <Card title="Позиция">
        <Empty>Позиции нет.</Empty>
      </Card>
    );
  const long = num(p.size) > 0;
  return (
    <Card title="Позиция">
      <div className="stats-grid">
        <Stat label="Направление" value={long ? "Long" : "Short"} tone={long ? "up" : "down"} />
        <Stat label="Размер" value={`${Math.abs(num(p.size))} BTC`} />
        <Stat label="Цена входа" value={price(p.entry_price)} />
        <Stat label="Прибыль сейчас" value={`${signed(p.unrealized_pnl, 4)}`} sub="USDC" tone={tone(p.unrealized_pnl)} />
        <Stat label="Цена ликвидации" value={price(p.liquidation_price)} />
        <Stat label="Плечо" value={p.leverage} />
      </div>
    </Card>
  );
}

function ExchangeOrders({ account }: { account: Account | null }) {
  if (!account) return <Card title="Ордера на бирже"><Loading /></Card>;
  const orders = account.open_orders;
  return (
    <Card title="Ордера на бирже" hint="Что сейчас стоит в книге Hyperliquid. У открытой позиции должен быть защитный стоп.">
      {orders.length === 0 ? (
        <Empty>{account.position ? "⚠ Ордеров нет — позиция без стопа!" : "Ордеров нет."}</Empty>
      ) : (
        <ul className="list">
          {orders.map((o) => (
            <li key={o.oid} className="list-item">
              <div>
                <strong>{o.trigger_price ? "Защитный стоп" : "Лимитный ордер"}</strong>
                <span className="muted"> · {SIDE[o.side] ?? o.side} {o.size} BTC</span>
              </div>
              <span>{o.trigger_price ? `срабатывает на ${price(o.trigger_price)}` : price(o.limit_price)}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function TradesCard({ trades }: { trades: Trade[] | null }) {
  return (
    <Card
      title="Сделки"
      hint="Каждая сделка — от открытия до закрытия позиции. Проверяется: размер не больше риска, стоп стоял на всю позицию, номинал в пределах лимита."
    >
      {!trades ? (
        <Loading />
      ) : trades.length === 0 ? (
        <Empty>Сделок пока не было.</Empty>
      ) : (
        <div className="trades">
          {trades.map((t) => (
            <div key={t.opened_at} className="trade">
              <div className="trade-main">
                <Pill tone={t.side === "long" ? "up" : "down"}>{t.side === "long" ? "Long" : "Short"}</Pill>
                <div>
                  <div className="trade-prices">
                    {price(t.entry_price)} → {t.exit_price ? price(t.exit_price) : "открыта"}
                  </div>
                  <div className="muted small">
                    {dateTime(t.opened_at)}
                    {t.closed_at ? ` — ${dateTime(t.closed_at)}` : ""} · {money(t.notional)} USDC ·{" "}
                    {t.exit_kind ? `закрыта ${EXIT_KIND[t.exit_kind] ?? t.exit_kind}` : "в позиции"}
                  </div>
                </div>
              </div>
              <div className="trade-result">
                <div className={`trade-net ${tone(t.net)}`}>{signed(t.net, 4)} USDC</div>
                <div className="muted small">комиссии {money(t.fees, 4)}</div>
              </div>
              <div className="trade-checks">
                {t.checks.length > 0 ? (
                  t.checks.map((c, i) => <ErrorText key={i}>✗ {c}</ErrorText>)
                ) : t.bot_entry ? (
                  <span className="ok-text">✓ Размер и стоп в порядке</span>
                ) : null}
                {t.notes.map((n, i) => (
                  <span key={i} className="muted small">
                    {n}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function BotOrders({ orders }: { orders: Order[] | null }) {
  return (
    <Card title="Ордера бота" hint="Все ордера, которые бот создавал, включая отклонённые проверками до отправки.">
      {!orders ? (
        <Loading />
      ) : orders.length === 0 ? (
        <Empty>Ордеров не было.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Когда</th>
              <th>Что</th>
              <th className="num">Цена</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => {
              const st = ORDER_STATUS[o.status] ?? { label: o.status, tone: "idle" as const };
              return (
                <tr key={o.id} title={o.error ?? o.cloid}>
                  <td className="muted">{dateTime(o.created_at)}</td>
                  <td>
                    {ORDER_PURPOSE[o.purpose] ?? o.purpose}
                    <span className="muted"> · {SIDE[o.side] ?? o.side} {o.size}</span>
                  </td>
                  <td className="num">{price(o.trigger_price ?? o.avg_fill_price ?? o.price)}</td>
                  <td>
                    <Pill tone={st.tone}>{st.label}</Pill>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function Fills({ fills }: { fills: Fill[] | null }) {
  return (
    <Card title="Исполнения на бирже" hint="Реальные сделки на бирже по аккаунту, включая ручные.">
      {!fills ? (
        <Loading />
      ) : fills.length === 0 ? (
        <Empty>Исполнений не было.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Когда</th>
              <th>Что</th>
              <th className="num">Цена</th>
              <th className="num">PnL</th>
            </tr>
          </thead>
          <tbody>
            {fills.map((f) => (
              <tr key={f.tid}>
                <td className="muted">{dateTime(f.time)}</td>
                <td>
                  {FILL_DIRECTION[f.direction] ?? f.direction}
                  <span className="muted">
                    {" "}
                    · {f.size}
                    {f.bot_order ? "" : " · вручную"}
                  </span>
                </td>
                <td className="num">{price(f.price)}</td>
                <td className={`num ${tone(f.closed_pnl)}`}>{signed(f.closed_pnl, 4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
