// Команды CLI из панели: запуск на сервере отдельным процессом, вывод — построчно.

import { useEffect, useRef, useState } from "react";
import { ApiError, command, type JobCommand, type Jobs, type JobStatus } from "../api";
import { Card, ErrorText, Loading, Pill } from "../components/ui";
import { dateTime, duration } from "../lib/format";
import { useNow, usePoll } from "../lib/usePoll";

export function Commands() {
  const jobs = usePoll<Jobs>("/api/jobs", 2_000);
  const [error, setError] = useState<string | null>(null);
  if (!jobs.data) return <div className="page">{jobs.error ? <ErrorText>{jobs.error}</ErrorText> : <Loading />}</div>;
  const { commands, job } = jobs.data;
  const groups = [...new Set(commands.map((c) => c.group))];
  const running = job.state === "running";

  async function run(c: JobCommand, args: Record<string, string>) {
    if (c.sends_to_exchange && !window.confirm(`${c.label}: команда отправит подписанный запрос на биржу. Продолжить?`)) return;
    setError(null);
    try {
      await command(`/api/jobs/${c.name}`, { args }, crypto.randomUUID());
      jobs.reload();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <div className="page">
      <div className="row row-cmd">
        <div className="cmd-groups">
          {groups.map((g) => (
            <Card key={g} title={g}>
              <ul className="cmd-list">
                {commands
                  .filter((c) => c.group === g)
                  .map((c) => (
                    <CommandItem key={c.name} c={c} disabled={running} active={job.name === c.name && running} onRun={run} />
                  ))}
              </ul>
            </Card>
          ))}
        </div>
        <Output job={job} label={commands.find((c) => c.name === job.name)?.label} error={error} />
      </div>
    </div>
  );
}

function CommandItem({
  c,
  disabled,
  active,
  onRun,
}: {
  c: JobCommand;
  disabled: boolean;
  active: boolean;
  onRun: (c: JobCommand, args: Record<string, string>) => void;
}) {
  const [args, setArgs] = useState<Record<string, string>>(() =>
    Object.fromEntries(c.choices.map((ch) => [ch.name, ch.default])),
  );
  return (
    <li className="cmd-item">
      <div className="cmd-text">
        <strong>{c.label}</strong>
        {c.sends_to_exchange && <Pill tone="warn">запрос на биржу</Pill>}
        <div className="muted small">{c.what}</div>
      </div>
      <div className="cmd-run">
        {c.choices.map((ch) => (
          <select
            key={ch.name}
            title={ch.label}
            value={args[ch.name]}
            disabled={disabled}
            onChange={(e) => setArgs({ ...args, [ch.name]: e.target.value })}
          >
            {ch.values.map((v) => (
              <option key={v} value={v}>
                {ch.label}: {v}
              </option>
            ))}
          </select>
        ))}
        <button className="btn" disabled={disabled} onClick={() => onRun(c, args)}>
          {active ? "Выполняется…" : "Запустить"}
        </button>
      </div>
    </li>
  );
}

function Output({ job, label, error }: { job: JobStatus; label?: string; error: string | null }) {
  const now = useNow(1_000);
  const pre = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [job.log.length]);
  const tone = job.state === "running" ? "info" : job.state === "done" ? "ok" : job.state === "error" ? "bad" : "idle";
  const text = { running: "выполняется", done: "готово", error: "ошибка", idle: "нет запусков" }[job.state];
  const took = job.started_at
    ? ((job.finished_at ? new Date(job.finished_at).getTime() : now) - new Date(job.started_at).getTime()) / 1000
    : NaN;
  return (
    <Card
      title="Вывод"
      className="cmd-output"
      action={
        <span className="cmd-state">
          <Pill tone={tone}>{text}</Pill>
          {job.started_at && <span className="muted small">{dateTime(job.started_at)} · {duration(took)}</span>}
        </span>
      }
    >
      {error && <ErrorText>{error}</ErrorText>}
      {job.name ? (
        <>
          <div className="muted small cmd-title">
            {label ?? job.name}
            {Object.keys(job.args).length > 0 && ` · ${Object.entries(job.args).map(([k, v]) => `${k} ${v}`).join(", ")}`}
            {job.state === "running" && job.total ? ` · ${job.done} из ${job.total}` : ""}
          </div>
          <pre className="console" ref={pre}>
            {job.log.length ? job.log.join("\n") : "…"}
          </pre>
        </>
      ) : (
        <p className="muted">Выберите команду слева — её вывод появится здесь.</p>
      )}
    </Card>
  );
}
