import type { AccountOverview, Status, WickCoin, WickOverview } from "../api";
import { PnlChart } from "../components/PnlChart";
import { ScanCard } from "../components/ScanCard";
import { Card, Dot, Empty, ErrorText, Loading, Meter, Pill } from "../components/ui";
import { clock, dateTime, duration, money, num, px, signed, tone } from "../lib/format";
import type { Tone } from "../lib/labels";
import { useNow, usePoll } from "../lib/usePoll";

type Day = WickOverview["daily"][number];
type MmOverview = WickOverview;
const todayUtc = () => new Date().toISOString().slice(0, 10);
const pctOf = (a: number, b: number) => ((a / b - 1) * 100).toFixed(2);

export function Wick({ status }: { status: Status }) {
  const w = usePoll<WickOverview>("/api/wick", 4_000);
  const account = usePoll<AccountOverview>("/api/account", 5_000);
  if (!w.data) return <div className="page">{w.error ? <ErrorText>{w.error}</ErrorText> : <Loading />}</div>;
  const data = w.data;
  const today = data.daily.filter((d) => d.day === todayUtc());
  const fresh = data.live !== null && data.live.last_beat_age_s <= 60;
  const coins = fresh ? Object.keys(data.live!.coins) : [];
  return (
    <div className="page">
      <Hero data={data} status={status} today={today} />
      {!fresh ? (
        <CurrentAccount account={account} />
      ) : coins.length > 0 ? (
        <div className="coins">
          {coins.map((c) => (
            <CoinCard key={c} coin={c} s={data.live!.coins[c]} today={today.find((d) => d.coin === c)} />
          ))}
        </div>
      ) : (
        <Card title="Монеты">
          <Empty>Отбор монет пуст: <code>eratrade wick-scan</code>, затем <code>scripts/eratrade.sh wick-start</code>.</Empty>
        </Card>
      )}
      <ScanCard />
      <div className="row row-3-2">
        <Fills fills={data.fills} />
        <Daily rows={data.daily} />
      </div>
    </div>
  );
}

function CurrentAccount({ account }: { account: { data: AccountOverview | null; error: string | null } }) {
  if (!account.data) return <Card title="Сейчас на бирже">{account.error ? <ErrorText>{account.error}</ErrorText> : <Loading />}</Card>;
  const data = account.data;
  return (
    <Card title="Сейчас на бирже" hint={`Актуальные данные биржи: ${dateTime(data.time)}. Бот остановлен, его старые карточки скрыты.`}>
      {account.error && <ErrorText>{account.error}</ErrorText>}
      <p>Баланс счёта: <b>{money(data.account_value)} USDC</b> · доступно: <b>{money(data.withdrawable)} USDC</b></p>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Монета</th><th className="num">Цена</th><th className="num">Позиция</th><th className="num">Средняя</th><th className="num">Нереализованный PnL</th></tr></thead>
          <tbody>
            {data.positions.map((p) => (
              <tr key={p.coin}><td>{p.coin}</td><td className="num">{px(p.mid)}</td><td className="num">{p.size}</td><td className="num">{px(p.entry_price)}</td><td className={`num ${tone(p.unrealized_pnl)}`}>{signed(p.unrealized_pnl, 4)} USDC</td></tr>
            ))}
          </tbody>
        </table>
        {data.positions.length === 0 && <Empty>Открытых позиций нет.</Empty>}
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Ордер</th><th>Тип</th><th className="num">Цена</th><th className="num">Размер</th></tr></thead>
          <tbody>
            {data.orders.map((o) => (
              <tr key={o.oid}><td><span className={`side-tag ${o.side}`}>{o.side === "buy" ? "BUY" : "SELL"}</span> {o.coin}</td><td>{o.is_trigger ? "Стоп" : o.reduce_only ? "Только закрытие" : "Лимит"}</td><td className="num">{px(o.price)}</td><td className="num">{o.size}</td></tr>
            ))}
          </tbody>
        </table>
        {data.orders.length === 0 && <Empty>Открытых ордеров нет.</Empty>}
      </div>
    </Card>
  );
}

function stateOf(data: WickOverview, status: Status): { tone: Tone; title: string; sub: string } {
  const live = data.live;
  if (!live) return { tone: "idle", title: "Бот не запускался", sub: "scripts/eratrade.sh wick-start" };
  if (live.last_beat_age_s > 60)
    return { tone: "bad", title: "Бот не отвечает", sub: `последняя отметка ${Math.round(live.last_beat_age_s)} с назад` };
  if (live.state.startsWith("остановлен")) return { tone: "bad", title: "Остановлен", sub: live.state.replace("остановлен: ", "") };
  if (status.entries_blocked) return { tone: "bad", title: "Покупки запрещены", sub: status.block_reason ?? "" };
  if (live.quoting) return { tone: "ok", title: "Ловит проколы", sub: "заявки на покупку стоят ниже цены" };
  if (status.mode === "LIVE_PAUSED") return { tone: "warn", title: "Пауза", sub: "покупки сняты, продажа и стоп на бирже" };
  if (status.mode === "DISABLED") return { tone: "idle", title: "Выключен", sub: "заявок нет" };
  return { tone: "warn", title: "Покупки выключены", sub: live.state };
}

function Hero({ data, status, today }: { data: WickOverview; status: Status; today: Day[] }) {
  const now = useNow(5_000);
  const live = data.live;
  const fresh = live !== null && live.last_beat_age_s <= 60;
  const st = stateOf(data, status);
  const pnl = fresh ? num(live.pnl_today) : NaN;
  const limit = num(data.config.daily_loss_usd);
  const fills = today.reduce((a, d) => a + d.fills, 0);
  const volume = today.reduce((a, d) => a + num(d.volume), 0);
  const uptime = fresh ? (now - new Date(live.started_at).getTime()) / 1000 : NaN;
  return (
    <section className="hero">
      <div className="hero-main">
        <div className={`hero-state ${st.tone}`}>
          <Dot tone={st.tone} pulse={st.tone === "ok"} />
          <span>{st.title}</span>
        </div>
        <div className="hero-sub">{st.sub}</div>
        <div className="hero-pnl-label">{fresh ? "Результат за сутки (UTC)" : "Текущий результат бота недоступен"}</div>
        <div className={`hero-pnl ${tone(pnl)}`}>
          {fresh ? signed(pnl, 4) : "—"}
          <span className="hero-unit">USDC</span>
        </div>
        <div className="hero-facts">
          {fresh && <span>реализовано <b className={tone(live.realized_today)}>{signed(live.realized_today, 4)}</b></span>}
          <span>
            сделок <b>{fills}</b>
          </span>
          <span>
            оборот <b>${money(volume, 0)}</b>
          </span>
          {fresh && <span>работает {duration(uptime)}</span>}
        </div>
      </div>
      <div className="hero-chart">
        <div className="hero-chart-head">
          <span>Накопленный результат</span>
          <span className="muted">после комиссий, закрытые сделки</span>
        </div>
        <PnlChart curve={data.curve} />
      </div>
      <div className="hero-side">
        {limit > 0 && <Meter share={pnl < 0 ? -pnl / limit : 0} label="Дневной лимит убытка" right={`${pnl < 0 ? money(-pnl, 2) : "0,00"} / ${money(limit, 0)} USDC`} />}
        <Meter
          share={fresh ? live.actions_last_min / live.actions_limit : 0}
          label="Действий в минуту"
          right={fresh ? `${live.actions_last_min} / ${live.actions_limit}` : "—"}
        />
        <div className="side-stats">
          <div>
            <span className="muted">Баланс на последней отметке</span>
            <b>{fresh ? `${money(live.spot_usdc)} USDC` : "—"}</b>
          </div>
          <div>
            <span className="muted">Покупки по уровням</span>
            <b>{data.config.levels_usd.map((x) => money(x, 1)).join(" → ")} USDC</b>
          </div>
          <div>
            <span className="muted">Отбор монет</span>
            <b>{live?.scan_at ? dateTime(live.scan_at) : "—"}</b>
          </div>
        </div>
        {fresh && !live.ws && <p className="banner-error">Нет связи с биржей</p>}
      </div>
    </section>
  );
}

function Levels({ level, levels }: { level: number; levels: number }) {
  return (
    <span className="levels" title={`исполнено уровней: ${level} из ${levels}`}>
      {Array.from({ length: levels }, (_, i) => (
        <span key={i} className={`lvl ${i < level ? "on" : ""}`} />
      ))}
    </span>
  );
}

function OrderRow({ kind, label, price, sub, empty }: { kind: "bid" | "ask" | "stop"; label: string; price?: string | null; sub?: string; empty: string }) {
  if (!price)
    return (
      <div className={`quote ${kind === "stop" ? "ask" : kind} off`}>
        <span className="quote-side">{label}</span>
        <span className="muted">{empty}</span>
      </div>
    );
  return (
    <div className={`quote ${kind === "stop" ? "ask" : kind}`}>
      <span className="quote-side">{label}</span>
      <span className="quote-price num">{px(price)}</span>
      <span className="muted num">{sub}</span>
    </div>
  );
}

function CoinCard({ coin, s, today }: { coin: string; s: WickCoin; today: Day | undefined }) {
  const mid = num(s.mid);
  const pos = num(s.position);
  const entry = num(s.entry_px);
  return (
    <section className="card coin">
      <header className="coin-head">
        <div>
          <div className="coin-name">{coin}</div>
          <div className="coin-price num">{px(s.mid)}</div>
        </div>
        <div className="coin-badges">
          {s.retiring && <Pill tone="warn">выбыла из отбора: только закрытие</Pill>}
          {s.oversized && <Pill tone="bad">ручная торговля?</Pill>}
          {!!s.foreign_orders && <Pill tone="warn">чужих заявок: {s.foreign_orders}</Pill>}
          <Pill tone="info">шаг {(num(s.step) * 100).toFixed(1)}%</Pill>
          <Pill tone="ok">цель +{(num(s.tp) * 100).toFixed(1)}%</Pill>
        </div>
      </header>
      <div className="wick-pos">
        <div>
          <div className="muted small">Позиция</div>
          <div className="wick-pos-value">{pos > 0 ? `$${money(s.position_usd)}` : "нет"}</div>
          {pos > 0 && <div className="muted small num">средняя {px(s.entry_px)} · {pctOf(mid, entry)}% от неё</div>}
        </div>
        <div className="right">
          <div className="muted small">Уровни</div>
          <Levels level={s.level} levels={s.levels} />
          {pos > 0 && <div className={`small num ${tone(s.unrealized)}`}>{signed(s.unrealized, 4)} USDC</div>}
        </div>
      </div>
      <div className="quotes">
        <OrderRow
          kind="ask"
          label="Продажа"
          price={s.tp_order}
          sub={s.tp_order && entry ? `+${pctOf(num(s.tp_order), entry)}% от средней` : undefined}
          empty={pos > 0 ? "ставится…" : "после покупки"}
        />
        <OrderRow
          kind="bid"
          label={pos > 0 ? `Докупка ${s.level + 1}` : "Покупка"}
          price={s.buy?.price}
          sub={s.buy ? `${pctOf(num(s.buy.price), mid)}% от цены · $${money(num(s.buy.price) * num(s.buy.size))}` : undefined}
          empty={s.level >= s.levels ? "все уровни исполнены" : s.note || "нет"}
        />
        <OrderRow
          kind="stop"
          label="Стоп"
          price={s.stop_order}
          sub={s.stop_order ? `${pctOf(num(s.stop_order), mid)}% от цены` : undefined}
          empty={s.level >= s.levels ? "ставится…" : `после ${s.levels}-го уровня`}
        />
      </div>
      {s.oversized && <p className="note">{s.note}</p>}
      <div className="coin-foot">
        <span>
          сегодня: <b>{today?.fills ?? 0}</b> сделок · <b>${money(today?.volume ?? 0, 0)}</b>
        </span>
        <span className={tone(today?.net)}>
          <b>{signed(today?.net ?? 0, 4)}</b> USDC
        </span>
      </div>
    </section>
  );
}

function Fills({ fills }: { fills: MmOverview["fills"] }) {
  return (
    <Card title="Сделки" hint="Последние исполнения. Итог — закрытый результат минус комиссия.">
      {fills.length === 0 ? (
        <Empty>Сделок ещё не было.</Empty>
      ) : (
        <div className="table-wrap fills-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Время</th>
                <th>Монета</th>
                <th className="num">Цена</th>
                <th className="num hide-sm">Сумма</th>
                <th className="num">Итог</th>
              </tr>
            </thead>
            <tbody>
              {fills.slice(0, 50).map((f, i) => {
                const net = num(f.closed_pnl) - num(f.fee);
                return (
                  <tr key={i}>
                    <td className="muted num">{clock(f.time)}</td>
                    <td>
                      <span className={`side-tag ${f.side}`}>{f.side === "buy" ? "BUY" : "SELL"}</span> {f.coin}
                      {f.crossed && <Pill tone="warn">рынок</Pill>}
                    </td>
                    <td className="num">{px(f.price)}</td>
                    <td className="num hide-sm">${money(f.notional)}</td>
                    <td className={`num ${tone(net)}`}>{signed(net, 4)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Daily({ rows }: { rows: MmOverview["daily"] }) {
  const days = [...new Set(rows.map((r) => r.day))];
  return (
    <Card title="По дням" hint="UTC. Итог — закрытые сделки после комиссий, без открытой позиции.">
      {rows.length === 0 ? (
        <Empty>Пока пусто.</Empty>
      ) : (
        <ul className="days">
          {days.map((day) => {
            const items = rows.filter((r) => r.day === day);
            const net = items.reduce((a, r) => a + num(r.net), 0);
            return (
              <li key={day}>
                <div className="day-head">
                  <span>{day === todayUtc() ? "Сегодня" : day}</span>
                  <b className={tone(net)}>{signed(net, 4)}</b>
                </div>
                {items.map((r) => (
                  <div key={r.coin} className="day-row muted">
                    <span>{r.coin}</span>
                    <span className="num">
                      {r.fills} сделок · ${money(r.volume, 0)} · комиссии {money(r.fees, 3)}
                    </span>
                    <span className={`num ${tone(r.net)}`}>{signed(r.net, 4)}</span>
                  </div>
                ))}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

