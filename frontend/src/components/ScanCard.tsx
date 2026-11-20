// Поиск монет по кнопке: запускает eratrade wick-scan на сервере, показывает ход и кандидатов.

import { useState } from "react";
import { ApiError, command, type WickScan } from "../api";
import { dateTime, duration } from "../lib/format";
import { useNow, usePoll } from "../lib/usePoll";
import { Card, Empty, ErrorText, Loading, Meter, Pill } from "./ui";

const pct = (x: number | null | undefined, digits = 1) =>
  x == null ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;

export function ScanCard() {
  const [top, setTop] = useState(80);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const scan = usePoll<WickScan>("/api/wick/scan", 3_000);
  const now = useNow(1_000);
  const data = scan.data;
  const running = data?.job.state === "running";

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await command("/api/wick/scan", { top }, crypto.randomUUID());
      scan.reload();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const action = (
    <div className="scan-action">
      <select value={top} onChange={(e) => setTop(Number(e.target.value))} disabled={running || busy}>
        {[40, 80, 120].map((n) => (
          <option key={n} value={n}>
            топ-{n} рынков
          </option>
        ))}
      </select>
      <button className="btn btn-primary" onClick={start} disabled={running || busy}>
        {running ? "Ищет…" : "Найти новые монеты"}
      </button>
    </div>
  );

  return (
    <Card
      title="Поиск монет"
      hint="Проверяет рынки Hyperliquid с наибольшим оборотом на истории Binance: параметры подбираются на 12 месяцах, результат — на последних 6 месяцах и текущем месяце. Бот подхватывает новый отбор сам: новые монеты добавляет, с выбывших уходит, только закрыв позицию."
      action={action}
    >
      {error && <ErrorText>{error}</ErrorText>}
      {!data ? (
        <Loading />
      ) : (
        <>
          {running && (
            <div className="scan-progress">
              <Meter
                share={data.job.total ? data.job.done / data.job.total : 0}
                label={`Проверено ${data.job.done}${data.job.total ? ` из ${data.job.total}` : ""} рынков`}
                right={data.job.started_at ? duration((now - new Date(data.job.started_at).getTime()) / 1000) : ""}
              />
              <div className="muted small scan-log">{data.job.log[data.job.log.length - 1] ?? "запуск…"}</div>
            </div>
          )}
          {data.job.state === "error" && (
            <div className="banner-error">
              Поиск завершился с ошибкой:
              <pre className="scan-pre">{data.job.log.slice(-6).join("\n")}</pre>
            </div>
          )}
          {!data.result ? (
            <Empty>Отбора ещё не было.</Empty>
          ) : (
            <Results data={data} />
          )}
        </>
      )}
    </Card>
  );
}

function Results({ data }: { data: WickScan }) {
  const r = data.result!;
  const passed = r.coins.filter((c) => c.passed);
  const chosen = new Set(passed.slice(0, data.max_coins).map((c) => c.coin));
  const shown = r.coins.filter((c) => c.test_total != null).slice(0, 15);
  return (
    <>
      <p className="muted small scan-meta">
        Отбор от {dateTime(r.at)} · проверено {r.universe ?? r.coins.length} рынков · прошли {passed.length} · проверка{" "}
        {r.test ? `${r.test[0]} … ${r.test[1]}` : "—"} · бот берёт лучшие {data.max_coins}
      </p>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>#</th>
              <th>Монета</th>
              <th className="num">Результат</th>
              <th className="num">Просадка</th>
              <th className="num hide-sm">Циклов</th>
              <th className="num hide-sm">Шаг / цель</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {shown.map((c, i) => (
              <tr key={c.coin}>
                <td className="muted">{i + 1}</td>
                <td>
                  <strong>{c.coin}</strong>
                </td>
                <td className={`num ${(c.test_total ?? 0) > 0 ? "up" : "down"}`}>{pct(c.test_total)}</td>
                <td className="num muted">{pct(c.test_drawdown)}</td>
                <td className="num hide-sm">{c.test_cycles}</td>
                <td className="num hide-sm muted">
                  {pct(c.step)} / {pct(c.tp)}
                </td>
                <td className="right">
                  {data.trading.includes(c.coin) ? (
                    <Pill tone="ok">торгуем</Pill>
                  ) : chosen.has(c.coin) ? (
                    <Pill tone="info">в отборе</Pill>
                  ) : c.passed ? (
                    <Pill tone="idle">прошла</Pill>
                  ) : (
                    <Pill tone="bad">не прошла</Pill>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
