import type { AuditEntry, Readiness, Status } from "../api";
import { Card, Dot, Empty, Loading, Pill } from "../components/ui";
import { ago, dateTime } from "../lib/format";
import {
  COMPONENTS,
  MODE_LABEL,
  actorText,
  auditText,
  checkName,
  componentState,
  type Tone,
} from "../lib/labels";
import { usePoll } from "../lib/usePoll";

export function System({ status }: { status: Status }) {
  const ready = usePoll<Readiness>("/api/live/readiness", 120_000);
  const audit = usePoll<AuditEntry[]>("/api/audit?limit=40", 30_000);

  return (
    <div className="page">
      <div className="row row-1-1">
        <Services status={status} />
        <Reconciliation status={status} />
      </div>
      <ReadinessCard ready={ready.data} error={ready.error} />
      <Journal entries={audit.data} />
    </div>
  );
}

function Services({ status }: { status: Status }) {
  const known = ["collector", "reconciler", "shadow", "trader"];
  const byName = Object.fromEntries(status.components.map((c) => [c.component, c]));
  const candleTone: Tone = (status.data.last_closed_age_min ?? 999) < 70 ? "ok" : "bad";
  return (
    <Card title="Сервисы" hint="Фоновые процессы в Docker. Каждый отмечается раз в 15–60 секунд.">
      <ul className="list">
        {known.map((name) => {
          const c = byName[name];
          const info = COMPONENTS[name];
          const state = c ? componentState(name, c.last_beat_age_s, status.mode) : { tone: "idle" as Tone, text: "ни разу не запускался" };
          return (
            <li key={name} className="list-item">
              <div>
                <Dot tone={state.tone} />
                <strong>{info.name}</strong>
                <div className="muted small indent">{info.hint}</div>
              </div>
              <div className="right">
                <div>{state.text}</div>
                {c && <div className="muted small">отметка {ago(c.last_beat_age_s)}</div>}
              </div>
            </li>
          );
        })}
        <li className="list-item">
          <div>
            <Dot tone={candleTone} />
            <strong>Рыночные данные</strong>
            <div className="muted small indent">Последняя закрытая часовая свеча</div>
          </div>
          <div className="right">
            <div>{dateTime(status.data.last_closed_candle)}</div>
            <div className="muted small">funding {status.data.funding ?? "—"}</div>
          </div>
        </li>
        <li className="list-item">
          <div>
            <Dot tone="info" />
            <strong>Режим работы</strong>
            <div className="muted small indent">изменил {actorText(status.mode_updated_by ?? "—")}</div>
          </div>
          <div className="right">
            <div>{MODE_LABEL[status.mode] ?? status.mode}</div>
            <div className="muted small">{dateTime(status.mode_updated_at)}</div>
          </div>
        </li>
      </ul>
    </Card>
  );
}

function Reconciliation({ status }: { status: Status }) {
  const rec = status.reconciliation;
  const visible = rec?.differences.filter((d) => !d.repaired || d.severity !== "info") ?? [];
  const tone: Tone = !rec ? "idle" : rec.status === "ok" ? "ok" : rec.status === "warnings" ? "warn" : "bad";
  return (
    <Card
      title="Сверка с биржей"
      hint="Раз в минуту бот сравнивает свою базу с биржей: позицию, ордера, сделки, funding. Любое расхождение — повод остановиться и разобраться."
    >
      {!rec ? (
        <Empty>Сверка ещё не запускалась.</Empty>
      ) : (
        <>
          <div className="recon-head">
            <Dot tone={tone} />
            <strong>
              {rec.status === "ok" ? "Всё совпадает" : rec.status === "warnings" ? "Есть предупреждения" : "Есть расхождения"}
            </strong>
            <span className="muted"> · {dateTime(rec.finished_at)}</span>
          </div>
          {visible.length === 0 ? (
            <p className="muted">База и биржа совпадают: позиция, ордера, сделки, funding.</p>
          ) : (
            <ul className="diffs">
              {visible.map((d, i) => (
                <li key={i} className={d.severity}>
                  {d.repaired ? "исправлено: " : ""}
                  {d.message}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}

const VERDICT: Record<string, { text: string; tone: Tone }> = {
  ok: { text: "Можно включать торговлю с ручным подтверждением", tone: "ok" },
  warn: { text: "Можно, но разберите предупреждения", tone: "warn" },
  fail: { text: "Торговлю включать нельзя", tone: "bad" },
};

function ReadinessCard({ ready, error }: { ready: Readiness | null; error: string | null }) {
  return (
    <Card
      title="Готовность к реальной торговле"
      hint="Автоматический чек-лист перед включением торговли (регламент docs/runbook.md)."
      action={ready && <Pill tone={VERDICT[ready.summary].tone}>{VERDICT[ready.summary].text}</Pill>}
    >
      {!ready ? (
        error ? <p className="error-text">{error}</p> : <Loading />
      ) : (
        <ul className="checks">
          {ready.checks.map((c) => (
            <li key={c.name} className={`check ${c.status}`}>
              <span className="check-mark">{c.status === "ok" ? "✓" : c.status === "warn" ? "!" : "✗"}</span>
              <div>
                <div className="check-name">{checkName(c.name)}</div>
                <div className="muted small">{c.detail}</div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Journal({ entries }: { entries: AuditEntry[] | null }) {
  return (
    <Card title="Журнал действий" hint="Все изменения режима, запреты, аварийные команды и смены моделей — кто, когда и почему.">
      {!entries ? (
        <Loading />
      ) : entries.length === 0 ? (
        <Empty>Записей нет.</Empty>
      ) : (
        <ul className="journal">
          {entries.map((a) => (
            <li key={a.id}>
              <span className="muted journal-time">{dateTime(a.occurred_at)}</span>
              <div>
                <strong>{auditText(a.action)}</strong>
                {a.reason && <span className="muted"> — {a.reason}</span>}
                <div className="muted small">{actorText(a.actor)}</div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
