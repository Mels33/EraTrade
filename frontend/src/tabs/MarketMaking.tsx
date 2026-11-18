import type { MmCoin, MmOverview, Status } from "../api";
import { PnlChart } from "../components/PnlChart";
import { Card, CenterGauge, Dot, Empty, ErrorText, Loading, Meter, Pill } from "../components/ui";
import { clock, duration, money, num, px, signed, tone } from "../lib/format";
import type { Tone } from "../lib/labels";
import { useNow, usePoll } from "../lib/usePoll";

type Day = MmOverview["daily"][number];
const todayUtc = () => new Date().toISOString().slice(0, 10);

export function MarketMaking({ status }: { status: Status }) {
  const mm = usePoll<MmOverview>("/api/mm", 4_000);
  if (!mm.data) return <div className="page">{mm.error ? <ErrorText>{mm.error}</ErrorText> : <Loading />}</div>;
  const data = mm.data;
  const today = data.daily.filter((d) => d.day === todayUtc());
  const coins = data.live ? Object.keys(data.live.coins) : data.config.coins;
  return (
    <div className="page">
      <Hero data={data} status={status} today={today} />
      <div className="coins">
        {coins.map((c) => (
          <CoinCard
            key={c}
            coin={c}
            state={data.live?.coins[c]}
            maxInventory={num(data.config.max_inventory_usd)}
            today={today.find((d) => d.coin === c)}
            stale={!data.live || data.live.last_beat_age_s > 60}
          />
        ))}
      </div>
      {data.live?.auto_select && <Selection live={data.live} active={coins} />}
      <div className="row row-3-2">
        <Fills fills={data.fills} />
        <Daily rows={data.daily} />
      </div>
    </div>
  );
}

function stateOf(data: MmOverview, status: Status): { tone: Tone; title: string; sub: string } {
  const live = data.live;
  if (!live) return { tone: "idle", title: "Маркет-мейкер не запускался", sub: "scripts/eratrade.sh mm-start" };
  if (live.last_beat_age_s > 60)
    return { tone: "bad", title: "Маркет-мейкер не отвечает", sub: `последняя отметка ${Math.round(live.last_beat_age_s)} с назад` };
  if (live.state.startsWith("остановлен")) return { tone: "bad", title: "Остановлен", sub: live.state.replace("остановлен: ", "") };
  if (status.entries_blocked) return { tone: "bad", title: "Входы запрещены", sub: status.block_reason ?? "" };
  if (live.quoting) return { tone: "ok", title: "Торгует", sub: "заявки стоят в стакане с обеих сторон" };
  if (status.mode === "LIVE_PAUSED") return { tone: "warn", title: "Пауза", sub: "заявки сняты" };
  if (status.mode === "DISABLED") return { tone: "idle", title: "Выключен", sub: "заявок нет" };
  return { tone: "warn", title: "Не котирует", sub: live.state };
}

function Hero({ data, status, today }: { data: MmOverview; status: Status; today: Day[] }) {
  const now = useNow(5_000);
  const live = data.live;
  const st = stateOf(data, status);
  const pnl = num(live?.pnl_today);
  const limit = num(data.config.daily_loss_usd);
  const fills = today.reduce((a, d) => a + d.fills, 0);
  const volume = today.reduce((a, d) => a + num(d.volume), 0);
  // Единый счёт: весь залог — USDC на споте; perp accountValue — только маржа позиций.
  const balance = live?.spot_usdc ?? live?.account_value;
  const uptime = live ? (now - new Date(live.started_at).getTime()) / 1000 : NaN;
  return (
    <section className="hero">
      <div className="hero-main">
        <div className={`hero-state ${st.tone}`}>
          <Dot tone={st.tone} pulse={st.tone === "ok"} />
          <span>{st.title}</span>
        </div>
        <div className="hero-sub">{st.sub}</div>

        <div className="hero-pnl-label">Результат за сутки (UTC)</div>
        <div className={`hero-pnl ${tone(pnl)}`}>
          {live ? signed(pnl, 4) : "—"}
          <span className="hero-unit">USDC</span>
        </div>
        <div className="hero-facts">
          <span>
            реализовано <b className={tone(live?.realized_today)}>{signed(live?.realized_today, 4)}</b>
          </span>
          <span>
            сделок <b>{fills}</b>
          </span>
          <span>
            оборот <b>${money(volume, 0)}</b>
          </span>
          {live && <span>работает {duration(uptime)}</span>}
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
        <Meter
          share={pnl < 0 ? -pnl / limit : 0}
          label="Дневной лимит убытка"
          right={`${pnl < 0 ? money(-pnl, 2) : "0,00"} / ${money(limit, 0)} USDC`}
        />
        <Meter
          share={live ? live.actions_last_min / live.actions_limit : 0}
          label="Действий в минуту"
          right={live ? `${live.actions_last_min} / ${live.actions_limit}` : "—"}
        />
        <div className="side-stats">
          <div>
            <span className="muted">Баланс</span>
            <b>{money(balance)} USDC</b>
          </div>
          <div>
            <span className="muted">Запросов адреса</span>
            <b>{live?.requests_remaining?.toLocaleString("ru-RU") ?? "—"}</b>
          </div>
          <div>
            <span className="muted">Заявка / предел</span>
            <b>
              {data.config.order_usd} / {data.config.max_inventory_usd} USDC
            </b>
          </div>
        </div>
        {live && !live.ws && <p className="banner-error">Нет связи с биржей — заявки сняты</p>}
        {live?.dms_error && (
          <p className="note" title={live.dms_error}>
            Автоотмена на бирже недоступна до оборота $1 млн: не выключайте компьютер с открытыми заявками.
          </p>
        )}
      </div>
    </section>
  );
}

function QuoteRow({ side, q, best }: { side: "bid" | "ask"; q: MmCoin["bid"]; best: number | null }) {
  const label = side === "bid" ? "Покупаем" : "Продаём";
  if (!q)
    return (
      <div className={`quote ${side} off`}>
        <span className="quote-side">{label}</span>
        <span className="muted">заявки нет</span>
      </div>
    );
  const p = num(q.price);
  const ahead = best != null && (side === "bid" ? p > best : p < best);
  return (
    <div className={`quote ${side}`}>
      <span className="quote-side">{label}</span>
      <span className="quote-price num">{px(q.price)}</span>
      <span className="muted num">
        × {q.size} · ${money(p * num(q.size), 2)}
      </span>
      {q.pending ? <Pill tone="idle">уточняется</Pill> : ahead ? <Pill tone="info">лучшая цена</Pill> : null}
    </div>
  );
}

function CoinCard({
  coin,
  state,
  maxInventory,
  today,
  stale,
}: {
  coin: string;
  state: MmCoin | undefined;
  maxInventory: number;
  today: Day | undefined;
  stale: boolean;
}) {
  if (!state)
    return (
      <Card title={coin}>
        <Loading />
      </Card>
    );
  const mid = num(state.mid);
  const half = state.spread_bps != null && Number.isFinite(mid) ? (mid * state.spread_bps) / 20_000 : null;
  const posUsd = num(state.position_usd);
  const flow = state.flow;
  const spreadTone = state.spread_bps == null ? "idle" : state.spread_bps >= 4 ? "ok" : "warn";
  return (
    <section className={`card coin ${stale ? "is-stale" : ""}`}>
      <header className="coin-head">
        <div>
          <div className="coin-name">{coin}</div>
          <div className="coin-price num">{px(state.mid)}</div>
        </div>
        <div className="coin-badges">
          {state.retiring && <Pill tone="warn">уходим: закрываем позицию</Pill>}
          <Pill tone={spreadTone}>спред {state.spread_bps != null ? `${state.spread_bps.toFixed(1)} bp` : "—"}</Pill>
          {state.book_age_s != null && state.book_age_s > 5 && <Pill tone="warn">стакан {Math.round(state.book_age_s)} с</Pill>}
        </div>
      </header>

      <div className="quotes">
        <QuoteRow side="ask" q={state.ask} best={half != null ? mid + half : null} />
        <QuoteRow side="bid" q={state.bid} best={half != null ? mid - half : null} />
      </div>

      <CenterGauge
        label="Позиция"
        value={posUsd}
        max={maxInventory}
        text={Number.isFinite(posUsd) && posUsd !== 0 ? `${signed(posUsd)} USDC · ${state.position}` : "нет"}
        left={`short ${maxInventory}`}
        right={`long ${maxInventory}`}
      />
      <CenterGauge
        label="Поток сделок за 30 с"
        value={flow ?? 0}
        max={1}
        text={flow == null ? "мало сделок" : `${flow > 0 ? "покупают" : "продают"} ${Math.abs(flow * 100).toFixed(0)}%`}
        left="продажи"
        right="покупки"
      />

      <div className="coin-foot">
        <span>
          сегодня: <b>{today?.fills ?? 0}</b> сделок · <b>${money(today?.volume ?? 0, 0)}</b>
        </span>
        <span className={tone(today?.net)}>
          <b>{signed(today?.net ?? 0, 4)}</b> USDC
        </span>
      </div>
      {state.reasons.length > 0 && (
        <div className="reasons">
          {state.reasons.map((r) => (
            <span key={r} className="reason">
              {r}
            </span>
          ))}
        </div>
      )}
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

function Selection({ live, active }: { live: NonNullable<MmOverview["live"]>; active: string[] }) {
  const now = useNow(30_000);
  const sel = live.selection ?? {};
  const cands = sel.candidates ?? [];
  const nextIn = sel.next_at ? (new Date(sel.next_at).getTime() - now) / 1000 : NaN;
  return (
    <Card
      title="Автовыбор монет"
      hint="Сканер оценивает топ-30 рынков по обороту: оценка = (спред − комиссии 3 bp) × √оборот. Текущую монету вытесняет только кандидат лучше на 30%. С вытесненной монеты бот уходит, закрывая позицию."
      action={
        <span className="muted small">
          {sel.at ? `пересмотр ${clock(sel.at)}` : "ещё не было"}
          {Number.isFinite(nextIn) && ` · следующий через ${duration(Math.max(0, nextIn))}`}
        </span>
      }
    >
      {cands.length === 0 ? (
        <Empty>Подходящих монет не найдено — бот остаётся на текущих.</Empty>
      ) : (
        <div className="cands">
          {cands.map((c, i) => {
            const chosen = (sel.target ?? []).includes(c.coin);
            const max = cands[0].score || 1;
            return (
              <div key={c.coin} className={`cand ${chosen ? "chosen" : ""}`}>
                <span className="cand-rank">{i + 1}</span>
                <span className="cand-coin">
                  {c.coin}
                  {chosen && <Pill tone="ok">{active.includes(c.coin) ? "торгуем" : "выбрана"}</Pill>}
                </span>
                <span className="num muted">спред {c.spread_bps.toFixed(1)} bp</span>
                <span className="num muted">${money(c.volume_usd / 1e6, 1)} млн/сут</span>
                <span className="cand-bar">
                  <span style={{ width: `${Math.max(4, (c.score / max) * 100)}%` }} />
                </span>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
