import type { AuditEntry, Status } from "../api";
import { Card, Dot, Empty, Loading } from "../components/ui";
import { ago, dateTime } from "../lib/format";
import { COMPONENTS, MODE_LABEL, actorText, auditText, componentState, modeTone, type Tone } from "../lib/labels";
import { usePoll } from "../lib/usePoll";

export function System({ status }: { status: Status }) {
  const audit = usePoll<AuditEntry[]>("/api/audit?limit=40", 30_000);
  return (
    <div className="page">
      <Services status={status} />
      <Journal entries={audit.data} />
    </div>
  );
}

function Services({ status }: { status: Status }) {
  const byName = Object.fromEntries(status.components.map((c) => [c.component, c]));
  const names = ["mm"];
  return (
    <Card title="Сервисы" hint="Фоновые процессы в Docker. Каждый отмечается раз в несколько секунд.">
      <ul className="list">
        <li className="list-item">
          <div className="li-title">
            <Dot tone={modeTone(status.mode, status.entries_blocked)} />
            <div>
              <strong>Режим</strong>
              <div className="muted small">изменил {actorText(status.mode_updated_by ?? "—")}</div>
            </div>
          </div>
          <div className="right">
            <div>{status.entries_blocked ? "Входы запрещены" : (MODE_LABEL[status.mode] ?? status.mode)}</div>
            <div className="muted small">{dateTime(status.mode_updated_at)}</div>
          </div>
        </li>
        {names.map((name) => {
          const c = byName[name];
          const info = COMPONENTS[name];
          const state = c ? componentState(name, c.last_beat_age_s) : { tone: "idle" as Tone, text: "не запускался" };
          return (
            <li key={name} className="list-item">
              <div className="li-title">
                <Dot tone={state.tone} />
                <div>
                  <strong>{info.name}</strong>
                  <div className="muted small">{info.hint}</div>
                </div>
              </div>
              <div className="right">
                <div>{state.text}</div>
                {c && <div className="muted small">{ago(c.last_beat_age_s)}</div>}
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function Journal({ entries }: { entries: AuditEntry[] | null }) {
  return (
    <Card title="Журнал действий" hint="Изменения режима, запреты и команды — кто, когда и почему.">
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
