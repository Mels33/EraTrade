// Базовые элементы интерфейса.

import type { ReactNode } from "react";
import type { Tone } from "../lib/labels";

export function Card({
  title,
  hint,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  hint?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <header className="card-head">
          <h2>
            {title}
            {hint && <Help text={hint} />}
          </h2>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Help({ text }: { text: string }) {
  return (
    <span className="help" title={text} aria-label={text} tabIndex={0}>
      ?
    </span>
  );
}

export function Dot({ tone, pulse = false }: { tone: Tone; pulse?: boolean }) {
  return <span className={`dot ${tone} ${pulse ? "pulse" : ""}`} aria-hidden />;
}

export function Pill({ tone, children }: { tone: Tone | "up" | "down" | "flat"; children: ReactNode }) {
  return <span className={`pill ${tone}`}>{children}</span>;
}

export function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "up" | "down" | "flat";
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ""}`}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Loading() {
  return <p className="empty">Загрузка…</p>;
}

export function ErrorText({ children }: { children: ReactNode }) {
  return <p className="error-text">{children}</p>;
}

/** Доля использования лимита: 0..1. */
export function Meter({ share, label, right }: { share: number; label: ReactNode; right: ReactNode }) {
  const s = Math.min(1, Math.max(0, Number.isFinite(share) ? share : 0));
  const tone = s >= 0.85 ? "bad" : s >= 0.5 ? "warn" : "ok";
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span className="num">{right}</span>
      </div>
      <div className="meter-track">
        <div className={`meter-fill ${tone}`} style={{ width: `${Math.max(s * 100, s > 0 ? 2 : 0)}%` }} />
      </div>
    </div>
  );
}

/** Шкала от −max до +max с заполнением от центра (позиция, поток сделок). */
export function CenterGauge({
  value,
  max,
  label,
  text,
  left,
  right,
}: {
  value: number;
  max: number;
  label: string;
  text: ReactNode;
  left: string;
  right: string;
}) {
  const v = Number.isFinite(value) ? Math.max(-1, Math.min(1, value / max)) : 0;
  const width = Math.abs(v) * 50;
  return (
    <div className="gauge">
      <div className="meter-head">
        <span>{label}</span>
        <span className="num">{text}</span>
      </div>
      <div className="gauge-track">
        <div className="gauge-mid" />
        <div
          className={`gauge-fill ${v >= 0 ? "pos" : "neg"}`}
          style={v >= 0 ? { left: "50%", width: `${width}%` } : { left: `${50 - width}%`, width: `${width}%` }}
        />
      </div>
      <div className="gauge-scale">
        <span>{left}</span>
        <span>0</span>
        <span>{right}</span>
      </div>
    </div>
  );
}
