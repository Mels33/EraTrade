// Графики на SVG без библиотек.

import { useState } from "react";
import type { Candle } from "../api";
import { dateTime, num, price } from "../lib/format";

/** Area-график цены закрытия с подсказкой при наведении. */
export function PriceChart({ candles, height = 180 }: { candles: Candle[]; height?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  if (candles.length < 2) return <p className="empty">Нет данных для графика</p>;

  const width = 600;
  const pad = { top: 12, bottom: 22, left: 0, right: 0 };
  const closes = candles.map((c) => num(c.close));
  const highs = candles.map((c) => num(c.high));
  const lows = candles.map((c) => num(c.low));
  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const span = max - min || 1;
  const x = (i: number) => pad.left + (i / (candles.length - 1)) * (width - pad.left - pad.right);
  const y = (v: number) => pad.top + (1 - (v - min) / span) * (height - pad.top - pad.bottom);

  const line = closes.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(closes.length - 1)},${height - pad.bottom} L${x(0)},${height - pad.bottom} Z`;
  const up = closes[closes.length - 1] >= closes[0];
  const color = up ? "var(--up)" : "var(--down)";
  const ticks = [0, Math.floor((candles.length - 1) / 2), candles.length - 1];
  const h = hover ?? null;

  return (
    <div className="chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const i = Math.round(((e.clientX - rect.left) / rect.width) * (candles.length - 1));
          setHover(Math.min(candles.length - 1, Math.max(0, i)));
        }}
        role="img"
        aria-label="График цены"
      >
        <defs>
          <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.28" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((f) => (
          <line
            key={f}
            x1={0}
            x2={width}
            y1={pad.top + f * (height - pad.top - pad.bottom)}
            y2={pad.top + f * (height - pad.top - pad.bottom)}
            className="grid-line"
          />
        ))}
        <path d={area} fill="url(#area)" />
        <path d={line} fill="none" stroke={color} strokeWidth={2} vectorEffect="non-scaling-stroke" />
        {h !== null && (
          <>
            <line x1={x(h)} x2={x(h)} y1={pad.top} y2={height - pad.bottom} className="cursor-line" />
            <circle cx={x(h)} cy={y(closes[h])} r={4} fill={color} vectorEffect="non-scaling-stroke" />
          </>
        )}
      </svg>
      <div className="chart-axis">
        {ticks.map((i) => (
          <span key={i}>{dateTime(candles[i].open_time)}</span>
        ))}
      </div>
      <div className="chart-range">
        <span>макс {price(max)}</span>
        <span>мин {price(min)}</span>
      </div>
      {h !== null && (
        <div className="chart-tip">
          {dateTime(candles[h].open_time)} · {price(candles[h].close)}
        </div>
      )}
    </div>
  );
}

const CLASSES = [
  { key: "DOWN", label: "Вниз", cls: "down" },
  { key: "FLAT", label: "Без движения", cls: "flat" },
  { key: "UP", label: "Вверх", cls: "up" },
] as const;

/** Три полосы вероятностей с подписями. */
export function ProbabilityBars({ probs }: { probs: Record<string, number> }) {
  return (
    <div className="probs">
      {CLASSES.map((c) => (
        <div key={c.key} className="prob-row">
          <span className="prob-label">{c.label}</span>
          <div className="prob-track">
            <div className={`prob-fill ${c.cls}`} style={{ width: `${(probs[c.key] ?? 0) * 100}%` }} />
          </div>
          <span className="prob-value">{Math.round((probs[c.key] ?? 0) * 100)}%</span>
        </div>
      ))}
    </div>
  );
}

/** Узкая составная полоса для таблиц. */
export function ProbabilityStrip({ probs }: { probs: Record<string, number> }) {
  return (
    <div
      className="strip"
      title={CLASSES.map((c) => `${c.label} ${Math.round((probs[c.key] ?? 0) * 100)}%`).join(" · ")}
    >
      {CLASSES.map((c) => (
        <div key={c.key} className={`prob-fill ${c.cls}`} style={{ width: `${(probs[c.key] ?? 0) * 100}%` }} />
      ))}
    </div>
  );
}

/** Уверенность модели по часам: рост — вверх от центра, падение — вниз; пунктир — порог сделки. */
export function ProbabilityHistory({
  items,
  threshold,
}: {
  items: { time: string; probs: Record<string, number> | null }[];
  threshold: number;
}) {
  if (items.length === 0) return <p className="empty">Сигналов ещё нет</p>;
  const pct = (v: number) => `${Math.round(v * 100)}%`;
  return (
    <div className="history">
      <div className="history-legend">
        <span><i className="sw up" /> уверенность в росте</span>
        <span><i className="sw down" /> уверенность в падении</span>
        <span><i className="sw line" /> порог для сделки {pct(threshold)}</span>
      </div>
      <div className="history-plot">
        <div className="history-threshold" style={{ top: `${50 - threshold * 50}%` }} />
        <div className="history-threshold" style={{ top: `${50 + threshold * 50}%` }} />
        <div className="history-axis" />
        {items.map((it, i) => (
          <div
            key={i}
            className={`history-col ${it.probs ? "" : "missing"}`}
            title={
              it.probs
                ? `${dateTime(it.time)}: рост ${pct(it.probs.UP)}, падение ${pct(it.probs.DOWN)}, без движения ${pct(it.probs.FLAT)}`
                : `${dateTime(it.time)}: прогноза нет`
            }
          >
            <div className="half top">
              {it.probs && <div className="prob-fill up" style={{ height: `${it.probs.UP * 100}%` }} />}
            </div>
            <div className="half bottom">
              {it.probs && <div className="prob-fill down" style={{ height: `${it.probs.DOWN * 100}%` }} />}
            </div>
          </div>
        ))}
      </div>
      <div className="chart-axis">
        <span>{dateTime(items[0].time)}</span>
        <span>{dateTime(items[items.length - 1].time)}</span>
      </div>
    </div>
  );
}
