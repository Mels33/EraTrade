// Управление ботом: панель действий и диалог подтверждения.

import { useState } from "react";
import { ApiError, command, type CommandResult } from "../api";

export interface CommandSpec {
  id: string;
  label: string;
  path: string;
  confirm?: string; // слово, которое нужно ввести
  danger: boolean;
  needsEngine: boolean;
  what: string; // что произойдёт
  when: string; // когда использовать
}

export const COMMANDS: CommandSpec[] = [
  {
    id: "pause",
    label: "Пауза",
    path: "/api/bot/pause",
    danger: false,
    needsEngine: false,
    what: "Запрещает новые сделки. Открытая позиция и её стоп остаются, закрыть её можно.",
    when: "Что-то выглядит странно и нужно время разобраться.",
  },
  {
    id: "resume",
    label: "Снять запрет входов",
    path: "/api/bot/resume",
    danger: false,
    needsEngine: false,
    what: "Снимает запрет новых сделок. Режим работы не меняется.",
    when: "Причина запрета разобрана и устранена.",
  },
  {
    id: "flatten",
    label: "Закрыть позицию",
    path: "/api/bot/flatten",
    confirm: "FLATTEN",
    danger: true,
    needsEngine: true,
    what: "Закрывает позицию по рынку и снимает её защитный стоп.",
    when: "Нужно выйти из сделки прямо сейчас.",
  },
  {
    id: "cancel",
    label: "Отменить все ордера",
    path: "/api/bot/cancel-all",
    confirm: "CANCEL_ALL",
    danger: true,
    needsEngine: true,
    what: "Отменяет все ордера на бирже, включая защитный стоп. Позиция останется без защиты!",
    when: "Только если на бирже висят лишние ордера.",
  },
  {
    id: "kill",
    label: "Аварийная остановка",
    path: "/api/bot/kill-switch",
    confirm: "KILL",
    danger: true,
    needsEngine: true,
    what: "Пауза → закрыть позицию → отменить ордера → бот выключен.",
    when: "Авария: нужно немедленно всё остановить.",
  },
];

export function ControlsPanel({
  tradingAvailable,
  onPick,
  onClose,
  onLogout,
}: {
  tradingAvailable: boolean;
  onPick: (c: CommandSpec) => void;
  onClose: () => void;
  onLogout: () => void;
}) {
  return (
    <div className="overlay" role="dialog" aria-modal onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <header className="sheet-head">
          <h3>Управление ботом</h3>
          <button className="icon-btn" onClick={onClose} aria-label="Закрыть">
            ✕
          </button>
        </header>
        <div className="actions-list">
          {COMMANDS.map((c) => {
            const disabled = c.needsEngine && !tradingAvailable;
            return (
              <button
                key={c.id}
                className={`action-item ${c.danger ? "danger" : ""}`}
                disabled={disabled}
                onClick={() => onPick(c)}
              >
                <span className="action-title">{c.label}</span>
                <span className="action-what">{c.what}</span>
                <span className="action-when">
                  {disabled ? "Недоступно: ключ API-кошелька не подключён к панели" : c.when}
                </span>
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
      if (spec.confirm) body.confirm = typed;
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
        <h3 className={spec.danger ? "danger-text" : ""}>{spec.label}</h3>
        <p className="dialog-what">{spec.what}</p>
        {!result && (
          <>
            <label>
              Причина — попадёт в журнал действий
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="например: разбираю расхождение со сверкой"
                autoFocus
              />
            </label>
            {spec.confirm && (
              <label>
                Чтобы подтвердить, введите <code>{spec.confirm}</code>
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
            {result.warnings?.map((w, i) => (
              <li key={`w${i}`} className="error-text">
                ⚠ {w}
              </li>
            ))}
          </ul>
        )}
        <div className="dialog-actions">
          <button className="btn" onClick={onClose}>
            {result ? "Готово" : "Отмена"}
          </button>
          {!result && (
            <button className={`btn ${spec.danger ? "btn-danger" : "btn-primary"}`} disabled={!ready} onClick={submit}>
              {busy ? "Выполняется…" : spec.label}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
