import { useEffect, useState } from "react";
import { getToken, setToken, type ModelVersion, type Status } from "./api";
import { CommandDialog, ControlsPanel, type CommandSpec } from "./components/controls";
import { MODE_LABEL } from "./lib/labels";
import { usePoll } from "./lib/usePoll";
import { Model } from "./tabs/Model";
import { Overview } from "./tabs/Overview";
import { System } from "./tabs/System";
import { Trading } from "./tabs/Trading";

const TABS = [
  { id: "overview", label: "Обзор" },
  { id: "trading", label: "Торговля" },
  { id: "model", label: "Модель" },
  { id: "system", label: "Система" },
] as const;
type TabId = (typeof TABS)[number]["id"];

function currentTab(): TabId {
  const id = window.location.hash.replace("#", "");
  return (TABS.find((t) => t.id === id)?.id ?? "overview") as TabId;
}

export default function App() {
  const [token, setTokenState] = useState(getToken());
  if (!token) {
    return (
      <Login
        onLogin={(t) => {
          setToken(t);
          setTokenState(t);
        }}
      />
    );
  }
  return (
    <Dashboard
      onLogout={(message) => {
        setToken(null);
        setTokenState(null);
        if (message) sessionStorage.setItem("eratrade.login_error", message);
      }}
    />
  );
}

function Dashboard({ onLogout }: { onLogout: (message?: string) => void }) {
  const [tab, setTab] = useState<TabId>(currentTab());
  const [controlsOpen, setControlsOpen] = useState(false);
  const [dialog, setDialog] = useState<CommandSpec | null>(null);
  const status = usePoll<Status>("/api/status", 5_000);
  const models = usePoll<{ items: ModelVersion[] }>("/api/models", 60_000);

  useEffect(() => {
    const onHash = () => setTab(currentTab());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (status.error?.includes("токен")) onLogout("Токен не принят");
  }, [status.error, onLogout]);

  const s = status.data;
  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="logo">E</span>
            <span className="brand-name">EraTrade</span>
            {s && (
              <>
                <span className="brand-meta">
                  {s.coin} · {s.interval}
                </span>
                <span className={`net-badge ${s.network}`}>{s.network === "mainnet" ? "MAINNET" : "TESTNET"}</span>
                <span className="mode-badge">{MODE_LABEL[s.mode] ?? s.mode}</span>
              </>
            )}
          </div>
          <nav className="tabs" aria-label="Разделы">
            {TABS.map((t) => (
              <a key={t.id} href={`#${t.id}`} className={`tab ${tab === t.id ? "active" : ""}`}>
                {t.label}
              </a>
            ))}
          </nav>
          <div className="topbar-actions">
            <button className="btn btn-control" onClick={() => setControlsOpen(true)}>
              Управление
            </button>
            <button className="btn btn-ghost logout" onClick={() => onLogout()} title="Выйти">
              Выйти
            </button>
          </div>
        </div>
      </header>

      <main className="content">
        {status.error && !s && <p className="banner-error">Нет связи с API: {status.error}</p>}
        {status.error && s && <p className="banner-error">Связь с API прервалась, данные могут быть устаревшими</p>}
        {!s ? (
          <p className="empty">Загрузка…</p>
        ) : tab === "overview" ? (
          <Overview status={s} models={models.data?.items ?? null} />
        ) : tab === "trading" ? (
          <Trading />
        ) : tab === "model" ? (
          <Model models={models.data?.items ?? null} />
        ) : (
          <System status={s} />
        )}
      </main>

      {controlsOpen && (
        <ControlsPanel
          tradingAvailable={s?.trading_commands_available ?? false}
          onClose={() => setControlsOpen(false)}
          onLogout={() => onLogout()}
          onPick={(c) => {
            setControlsOpen(false);
            setDialog(c);
          }}
        />
      )}
      {dialog && (
        <CommandDialog
          spec={dialog}
          onClose={() => setDialog(null)}
          onDone={() => {
            status.reload();
            models.reload();
          }}
        />
      )}
    </div>
  );
}

function Login({ onLogin }: { onLogin: (t: string) => void }) {
  const [value, setValue] = useState("");
  const [error] = useState(() => {
    const e = sessionStorage.getItem("eratrade.login_error");
    sessionStorage.removeItem("eratrade.login_error");
    return e;
  });
  return (
    <main className="login">
      <div className="login-card">
        <span className="logo big">E</span>
        <h1>EraTrade</h1>
        <p className="muted">Панель оператора торгового бота</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (value.trim()) onLogin(value.trim());
          }}
        >
          <label>
            Токен оператора
            <input
              type="password"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="API_TOKEN из .env.mainnet.secrets"
              autoFocus
            />
          </label>
          <button type="submit" className="btn btn-primary">
            Войти
          </button>
        </form>
        {error && <p className="error-text">{error}</p>}
        <p className="muted small">Токен хранится только в этой вкладке браузера.</p>
      </div>
    </main>
  );
}
