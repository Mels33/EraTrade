// Управление ботом: панель действий и диалог подтверждения.

import { useState } from "react";
import { ApiError, command, type CommandResult, type Status } from "../api";

const PHRASE = "I_UNDERSTAND_REAL_MONEY";

export interface CommandSpec {
  id: "start" | "pause" | "resume" | "stop";
  label: string;
  path: string;
  tone: "primary" | "warn" | "danger";
  confirm?: string; // слово, которое нужно ввести
  phrase?: boolean; // отправить слово как confirmation_phrase
  what: string;
}

export const COMMANDS: CommandSpec[] = [
  {
    id: "start",
    label: "Запустить торговлю",
    path: "/api/bot/start",
    tone: "primary",
    confirm: PHRASE,
    phrase: true,
    what: "Снимает запрет входов и включает режим «Торгует»: маркет-мейкер начинает ставить заявки реальными деньгами.",
  },
  {
    id: "pause",
    label: "Пауза",
    path: "/api/bot/pause",
    tone: "warn",
    what: "Маркет-мейкер снимает все заявки. Открытая позиция остаётся — её можно закрыть кнопкой «Остановить».",
  },
  {
    id: "resume",
    label: "Снять запрет входов",
    path: "/api/bot/resume",
    tone: "warn",
    what: "Снимает запрет после разбора (лимит убытка, пауза). Режим не меняется: чтобы торговать, нужен режим «Торгует».",
  },
  {
    id: "stop",
    label: "Остановить и закрыть позиции",
    path: "/api/bot/stop",
    tone: "danger",
    confirm: "STOP",
    what: "Режим «Выключен»: заявки снимаются, позиции маркет-мейкера закрываются по рынку.",
  },
];

function available(c: CommandSpec, s: Status): boolean {
  const live = s.mode === "LIVE_ENABLED" && !s.entries_blocked;
  if (c.id === "start") return !live;
  if (c.id === "pause") return live;
  if (c.id === "resume") return s.entries_blocked;
  return s.mode !== "DISABLED";
}

export function ControlsPanel({
  status,
  onPick,
  onClose,
  onLogout,
}: {
  status: Status;
  onPick: (c: CommandSpec) => void;
  onClose: () => void;
  onLogout: () => void;
}) {
  return (
    <div className="overlay" role="dialog" aria-modal onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <header className="sheet-head">
          <h3>Управление</h3>
          <button className="icon-btn" onClick={onClose} aria-label="Закрыть">
            ✕
          </button>
        </header>
        <div className="actions-list">
          {COMMANDS.map((c) => {
            const ok = available(c, status);
            return (
              <button key={c.id} className={`action-item ${c.tone}`} disabled={!ok} onClick={() => onPick(c)}>
                <span className="action-title">{c.label}</span>
                <span className="action-what">{c.what}</span>
                {!ok && <span className="action-when">Сейчас не нужно</span>}
              </button>
            );
          })}
        </div>
        <footer className="sheet-foot">
          <button className="btn btn-ghost" onClick={onLogout}>
            Выйти из панели
          </button>
        </footer>
      </div>
    </div>
  );
}

export function CommandDialog({
  spec,
  onClose,
  onDone,
}: {
  spec: CommandSpec;
  onClose: () => void;
  onDone: () => void;
}) {
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
      if (spec.phrase) body.confirmation_phrase = typed;
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
        <h3 className={spec.tone === "danger" ? "danger-text" : ""}>{spec.label}</h3>
        <p className="dialog-what">{spec.what}</p>
        {!result && (
          <>
            <label>
              Причина — попадёт в журнал
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="например: проверяю настройки"
                autoFocus
              />
            </label>
            {spec.confirm && (
              <label>
                Для подтверждения введите <code>{spec.confirm}</code>
                <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
              </label>
            )}
          </>
        )}
        {error && <p className="error-text">{error}</p>}
        {result && (
          <ul className="steps">
            {result.steps.map((s, i) => (
              <li key={i}>✓ {s}</li>
            ))}
          </ul>
        )}
        <div className="dialog-actions">
          <button className="btn" onClick={onClose}>
            {result ? "Готово" : "Отмена"}
          </button>
          {!result && (
            <button className={`btn btn-${spec.tone}`} disabled={!ready} onClick={submit}>
              {busy ? "Выполняется…" : spec.label}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
