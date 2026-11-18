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
  title?: string;
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

export function Dot({ tone }: { tone: Tone }) {
  return <span className={`dot ${tone}`} aria-hidden />;
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

/** Горизонтальная полоса: доля использования лимита. */
export function Meter({
  value,
  max,
  label,
  right,
  caption,
}: {
  value: number;
  max: number;
  label: string;
  right: ReactNode;
  caption?: ReactNode;
}) {
  const share = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  const tone = share >= 1 ? "bad" : share >= 0.6 ? "warn" : "ok";
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span>{right}</span>
      </div>
      <div className="meter-track">
        <div className={`meter-fill ${tone}`} style={{ width: `${Math.max(share * 100, share > 0 ? 2 : 0)}%` }} />
      </div>
      {caption && <div className="meter-caption">{caption}</div>}
    </div>
  );
}
