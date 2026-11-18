import type { ModelVersion, ShadowModel, ShadowReport, Signal } from "../api";
import { ProbabilityHistory, ProbabilityStrip } from "../components/charts";
import { Card, Empty, Loading, Meter, Pill, Stat } from "../components/ui";
import { dateTime, percent, signedPercent, tone } from "../lib/format";
import { DECISION, MODEL_STATUS, featureLabel, modelName, reasonText } from "../lib/labels";

const MIN_DRIFT_SNAPSHOTS = 24;
import { usePoll } from "../lib/usePoll";

export function Model({ models }: { models: ModelVersion[] | null }) {
  const report = usePoll<ShadowReport>("/api/shadow/report?days=30", 120_000);
  const signals = usePoll<{ items: Signal[] }>("/api/signals?limit=96", 60_000);

  const current = models?.find((m) => m.status === "active") ?? models?.find((m) => m.status === "shadow");
  const section = report.data?.models.find((m) => m.version === current?.version);
  // одна точка на час: сигнал торгующей модели, иначе наблюдаемой
  const byHour = new Map<string, Signal>();
  for (const s of signals.data?.items ?? []) {
    const prev = byHour.get(s.decision_time);
    if (!prev || (s.model_role === "active" && prev.model_role !== "active")) byHour.set(s.decision_time, s);
  }
  const hourly = [...byHour.values()].sort((a, b) => a.decision_time.localeCompare(b.decision_time)).slice(-72);

  return (
    <div className="page">
      {!report.data || !models ? (
        <Card title="Проверка модели">
          <Loading />
        </Card>
      ) : !current ? (
        <Card title="Проверка модели">
          <Empty>Моделей в работе нет. Обучить: eratrade train, затем eratrade model-shadow.</Empty>
        </Card>
      ) : (
        <Progress model={current} section={section ?? null} report={report.data} />
      )}
      <Card
        title="Уверенность модели по часам"
        hint="Каждый столбик — один час. Вверх — насколько модель уверена в росте, вниз — в падении. Сделка возможна, только когда столбик заходит за пунктир (и ожидаемая выгода выше издержек)."
      >
        {signals.data ? (
          <ProbabilityHistory
            items={hourly.map((s) => ({ time: s.decision_time, probs: s.probabilities }))}
            threshold={report.data?.min_probability ?? 0.45}
          />
        ) : (
          <Loading />
        )}
      </Card>
      <SignalFeed signals={signals.data?.items ?? null} />
      <Versions models={models} />
    </div>
  );
}

function Progress({ model, section, report }: { model: ModelVersion; section: ShadowModel | null; report: ShadowReport }) {
  const need = report.live_min_outcomes;
  const resolved = section?.calibration.resolved ?? 0;
  const cal = section?.calibration;
  const errors = section ? Object.values(section.feature_or_model_errors).reduce((a, b) => a + b, 0) : 0;
  const driftReady = report.snapshots >= MIN_DRIFT_SNAPSHOTS;
  const drift = driftReady
    ? (section?.drift.filter((d) => Math.abs(d.shift_sigma ?? 0) >= report.drift_sigma_warn) ?? [])
    : [];
  const st = MODEL_STATUS[model.status] ?? { label: model.status, tone: "idle" as const };
  const better = cal?.log_loss !== undefined && cal.baseline_log_loss !== undefined && cal.log_loss < cal.baseline_log_loss;

  return (
    <Card
      title="Проверка модели на живых данных"
      hint="Модель в режиме наблюдения каждый час делает прогноз, но не торгует. Через 4 часа прогноз сверяется с реальной ценой. Торговать ей можно, когда накопится достаточно проверок и она окажется точнее простого угадывания."
      action={<Pill tone={st.tone}>{st.label}</Pill>}
    >
      <p className="model-name">{modelName(model.version)}</p>
      <Meter
        label="Проверено прогнозов"
        value={resolved}
        max={need}
        right={`${resolved} из ${need}`}
        caption={
          resolved >= need
            ? "Достаточно для решения об активации"
            : `Ещё примерно ${need - resolved} ч наблюдения (каждый прогноз проверяется через 4 ч)`
        }
      />
      <div className="stats-grid">
        <Stat label="Сигналов" value={section?.signals ?? 0} />
        <Stat label="Покрыто часов" value={percent(section?.coverage ?? null)} />
        <Stat label="Пропусков из-за ошибок" value={errors} tone={errors ? "down" : undefined} />
        <Stat
          label="Решение после закрытия свечи"
          value={section?.latency_s.p50 != null ? `${Math.round(section.latency_s.p50)} с` : "—"}
        />
      </div>
      <div className="verdicts">
        {cal?.log_loss === undefined ? (
          <p className="muted">Точность появится после первых проверенных прогнозов.</p>
        ) : (
          <p className={better ? "ok-text" : "error-text"}>
            {better ? "✓ Точнее, чем угадывание по частотам" : "✗ Пока не точнее угадывания по частотам"}
            <span className="muted small">
              {" "}
              (log loss {cal.log_loss.toFixed(3)} против {cal.baseline_log_loss?.toFixed(3)}
              {cal.enough ? "" : `, мало данных — нужно ≥ ${report.min_outcomes_for_metrics}`})
            </span>
          </p>
        )}
        {section && section.entries.resolved > 0 && (
          <p>
            Сделки, которые модель открыла бы: {section.entries.resolved}, угадано{" "}
            {percent(section.entries.precision)}, в среднем{" "}
            <span className={tone(section.entries.mean_net_return)}>{signedPercent(section.entries.mean_net_return)}</span>{" "}
            после комиссий
          </p>
        )}
        {!driftReady ? (
          <p className="muted">
            Сравнение рынка с обучающими данными — после суток наблюдения ({report.snapshots} из {MIN_DRIFT_SNAPSHOTS} ч).
          </p>
        ) : drift.length > 0 ? (
          <p className="warn-text" title={drift.map((d) => `${d.feature}: ${d.shift_sigma?.toFixed(1)}σ`).join(", ")}>
            ⚠ Рынок сейчас отличается от обучающих данных: {[...new Set(drift.slice(0, 5).map((d) => featureLabel(d.feature)))].join(", ")}
          </p>
        ) : (
          <p className="muted">Рынок похож на обучающие данные.</p>
        )}
      </div>
    </Card>
  );
}

function SignalFeed({ signals }: { signals: Signal[] | null }) {
  return (
    <Card title="Сигналы" hint="Прогноз на 4 часа вперёд. «Итог» заполняется, когда 4 часа прошли: реальное изменение цены.">
      {!signals ? (
        <Loading />
      ) : signals.length === 0 ? (
        <Empty>Сигналов пока нет.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Час</th>
              <th>Решение</th>
              <th className="hide-sm">Вниз · без движения · вверх</th>
              <th className="hide-sm">Почему</th>
              <th className="num">Итог за 4 ч</th>
            </tr>
          </thead>
          <tbody>
            {signals.slice(0, 48).map((s) => {
              const d = DECISION[s.decision] ?? { label: s.decision, tone: "flat" as const };
              return (
                <tr key={s.id} title={`${modelName(s.model_version)} · ${s.reason}`}>
                  <td className="muted">
                    {dateTime(s.decision_time)}
                    {s.model_role === "shadow" && <span className="tag">набл.</span>}
                  </td>
                  <td>
                    <Pill tone={d.tone === "warn" ? "warn" : d.tone}>{d.label}</Pill>
                  </td>
                  <td className="hide-sm">{s.probabilities ? <ProbabilityStrip probs={s.probabilities} /> : "—"}</td>
                  <td className="hide-sm reason">{reasonText(s.reason)}</td>
                  <td className={`num ${tone(s.outcome_return)}`}>
                    {s.outcome_return === null ? <span className="muted">ждём</span> : signedPercent(s.outcome_return)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function Versions({ models }: { models: ModelVersion[] | null }) {
  return (
    <Card title="Версии моделей" hint="Торговать может только одна модель. Новая сначала проходит наблюдение.">
      {!models ? (
        <Loading />
      ) : models.length === 0 ? (
        <Empty>Моделей нет.</Empty>
      ) : (
        <ul className="list">
          {models.map((m) => {
            const st = MODEL_STATUS[m.status] ?? { label: m.status, tone: "idle" as const };
            const ll = m.test.log_loss;
            const base = m.test.baseline_log_loss;
            const gain = ll !== null && base !== null ? 1 - ll / base : null;
            return (
              <li key={m.version} className="list-item" title={m.version}>
                <div>
                  <strong>{modelName(m.version)}</strong>
                  <div className="muted small">
                    {gain === null
                      ? "нет метрик"
                      : gain > 0
                        ? `на истории точнее угадывания на ${(gain * 100).toFixed(1)}%`
                        : "на истории не точнее угадывания"}
                  </div>
                </div>
                <Pill tone={st.tone}>{st.label}</Pill>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
