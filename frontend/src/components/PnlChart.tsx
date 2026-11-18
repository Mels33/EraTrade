// Накопленный результат маркет-мейкера: ступенчатая площадь по времени.

import { useMemo, useState } from "react";
import { clock, num, signed } from "../lib/format";

interface Point {
  t: number;
  v: number;
}

export function PnlChart({ curve, height = 150 }: { curve: { t: string; pnl: string }[]; height?: number }) {
  const [hover, setHover] = useState<Point | null>(null);
  const width = 640;
  const pad = { top: 10, bottom: 18, left: 4, right: 4 };

  const pts = useMemo<Point[]>(() => {
    const raw = curve.map((c) => ({ t: new Date(c.t).getTime(), v: num(c.pnl) }));
    if (raw.length === 0) return [];
    // Старт с нуля за минуту до первой сделки и продолжение до «сейчас».
    return [{ t: raw[0].t - 60_000, v: 0 }, ...raw, { t: Date.now(), v: raw[raw.length - 1].v }];
  }, [curve]);

  if (pts.length < 2) {
    return <div className="chart-empty">График появится после первых сделок</div>;
  }

  const t0 = pts[0].t;
  const t1 = Math.max(pts[pts.length - 1].t, t0 + 60_000);
  const vs = pts.map((p) => p.v);
  const lo = Math.min(0, ...vs);
  const hi = Math.max(0, ...vs);
  const span = hi - lo || 1;
  const x = (t: number) => pad.left + ((t - t0) / (t1 - t0)) * (width - pad.left - pad.right);
  const y = (v: number) => pad.top + (1 - (v - lo) / span) * (height - pad.top - pad.bottom);

  let line = `M${x(pts[0].t)},${y(pts[0].v)}`;
  for (let i = 1; i < pts.length; i++) line += ` H${x(pts[i].t)} V${y(pts[i].v)}`;
  const area = `${line} V${y(lo)} H${x(pts[0].t)} Z`;
  const last = pts[pts.length - 1].v;
  const color = last >= 0 ? "var(--up)" : "var(--down)";

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const t = t0 + ((e.clientX - rect.left) / rect.width) * (t1 - t0);
    let best = pts[0];
    for (const p of pts) if (p.t <= t) best = p;
    setHover({ t, v: best.v });
  }

  const ticks = [t0, t0 + (t1 - t0) / 2, t1];
  return (
    <div className="chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="Накопленный результат"
      >
        <defs>
          <linearGradient id="pnl-fill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.35" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        <line x1={0} x2={width} y1={y(0)} y2={y(0)} className="chart-zero" />
        <path d={area} fill="url(#pnl-fill)" />
        <path d={line} fill="none" stroke={color} strokeWidth={2} vectorEffect="non-scaling-stroke" />
        {hover && <line x1={x(hover.t)} x2={x(hover.t)} y1={pad.top} y2={height - pad.bottom} className="chart-cursor" />}
      </svg>
      <div className="chart-axis">
        {ticks.map((t, i) => (
          <span key={i}>{clock(new Date(t).toISOString())}</span>
        ))}
      </div>
      {hover && (
        <div className="chart-tip">
          {clock(new Date(hover.t).toISOString())} · <strong className={hover.v >= 0 ? "up" : "down"}>{signed(hover.v, 4)}</strong> USDC
        </div>
      )}
    </div>
  );
}
