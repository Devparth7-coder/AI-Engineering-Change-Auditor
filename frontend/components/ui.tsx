"use client";

import { ReactNode, useEffect, useRef, useState } from "react";
import { Tone } from "@/lib/api";

const TONE_CLASS: Record<Tone, string> = {
  ok: "text-ok border-ok/40 bg-ok/10",
  bad: "text-bad border-bad/40 bg-bad/10",
  warn: "text-warn border-warn/40 bg-warn/10",
  info: "text-accent border-accent/40 bg-accent/10",
  vio: "text-vio border-vio/40 bg-vio/10",
  muted: "text-muted border-line bg-panel2",
};

const DOT_CLASS: Record<Tone, string> = {
  ok: "bg-ok",
  bad: "bg-bad",
  warn: "bg-warn",
  info: "bg-accent",
  vio: "bg-vio",
  muted: "bg-muted",
};

export function Badge({
  tone = "muted",
  children,
  mono = true,
}: {
  tone?: Tone;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-[11px] leading-4 ${
        mono ? "font-mono" : ""
      } ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

export function Dot({ tone = "muted", pulse = false }: { tone?: Tone; pulse?: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${DOT_CLASS[tone]} ${
        pulse ? "live-dot" : ""
      }`}
    />
  );
}

export function Card({
  title,
  subtitle,
  right,
  children,
  className = "",
  pad = true,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
  children?: ReactNode;
  className?: string;
  pad?: boolean;
}) {
  return (
    <section
      className={`rounded-lg border border-line bg-panel shadow-[0_1px_0_0_rgba(255,255,255,0.03)_inset] ${className}`}
    >
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <div className="min-w-0">
            {title && (
              <h2 className="truncate text-[13px] font-semibold tracking-wide text-fg">
                {title}
              </h2>
            )}
            {subtitle && <p className="truncate text-[11px] text-muted">{subtitle}</p>}
          </div>
          {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
        </header>
      )}
      <div className={pad ? "p-4" : ""}>{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  tone = "muted",
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: Tone;
  hint?: string;
}) {
  const valueTone: Record<Tone, string> = {
    ok: "text-ok",
    bad: "text-bad",
    warn: "text-warn",
    info: "text-accent",
    vio: "text-vio",
    muted: "text-fg",
  };
  return (
    <div className="rounded-lg border border-line bg-panel px-4 py-3">
      <div className="text-[10px] uppercase tracking-[0.14em] text-muted">{label}</div>
      <div className={`mt-1 font-mono text-2xl leading-7 ${valueTone[tone]}`}>{value}</div>
      {hint && <div className="mt-0.5 truncate text-[11px] text-muted">{hint}</div>}
    </div>
  );
}

export function KV({ k, v, mono = true }: { k: string; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex gap-3 py-0.5 text-[12px]">
      <span className="w-44 shrink-0 text-muted">{k}</span>
      <span className={`min-w-0 break-words ${mono ? "font-mono" : ""}`}>{v}</span>
    </div>
  );
}

export function Progress({ pct, tone = "info" }: { pct: number; tone?: Tone }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded bg-panel2">
      <div
        className={`bar-fill h-full rounded ${DOT_CLASS[tone]}`}
        style={{ width: `${Math.max(2, Math.min(100, pct))}%` }}
      />
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-[12px] text-muted">
      <Dot tone="info" pulse />
      <span className="font-mono">{label}</span>
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-line bg-panel/40 px-6 py-10 text-center">
      <p className="text-[13px] text-muted">{title}</p>
      {hint && <p className="mt-1 text-[11px] text-muted/70">{hint}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="rounded-lg border border-bad/40 bg-bad/10 px-4 py-3">
      <p className="font-mono text-[12px] text-bad">{message}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-2 rounded border border-bad/40 px-2 py-1 text-[11px] text-bad hover:bg-bad/10"
        >
          retry
        </button>
      )}
    </div>
  );
}

export function Button({
  children,
  onClick,
  tone = "info",
  disabled,
  busy,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  tone?: Tone;
  disabled?: boolean;
  busy?: boolean;
  title?: string;
}) {
  const styles: Record<Tone, string> = {
    ok: "border-ok/50 text-ok hover:bg-ok/10",
    bad: "border-bad/50 text-bad hover:bg-bad/10",
    warn: "border-warn/50 text-warn hover:bg-warn/10",
    info: "border-accent/50 text-accent hover:bg-accent/10",
    vio: "border-vio/50 text-vio hover:bg-vio/10",
    muted: "border-line text-muted hover:bg-panel2",
  };
  return (
    <button
      title={title}
      disabled={disabled || busy}
      onClick={onClick}
      className={`rounded border px-3 py-1.5 font-mono text-[12px] transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${styles[tone]}`}
    >
      {busy ? "working…" : children}
    </button>
  );
}

/** Explicit, blocking confirmation. Destructive/irreversible actions never fire
 *  from a single click. */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  tone = "warn",
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  tone?: Tone;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6">
      <div className="w-full max-w-lg rounded-lg border border-line bg-panel shadow-2xl">
        <div className="border-b border-line px-5 py-3">
          <h3 className="font-mono text-[13px] font-semibold text-fg">{title}</h3>
        </div>
        <div className="px-5 py-4 text-[12px] leading-5 text-muted">{body}</div>
        <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
          <Button tone="muted" onClick={onCancel}>
            cancel
          </Button>
          <Button tone={tone} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function Pre({ text, maxHeight = 320 }: { text: string; maxHeight?: number }) {
  return (
    <pre
      className="overflow-auto rounded border border-line bg-bg p-3 font-mono text-[11.5px] leading-[1.55] text-fg"
      style={{ maxHeight }}
    >
      {text}
    </pre>
  );
}

/** Unified-diff renderer: green additions, red deletions, violet hunk headers. */
export function DiffView({ text }: { text: string }) {
  return (
    <pre className="overflow-auto rounded border border-line bg-bg p-3 font-mono text-[11.5px] leading-[1.6]">
      {text.split("\n").map((ln, i) => {
        let cls = "text-fg";
        if (ln.startsWith("+++") || ln.startsWith("---")) cls = "text-muted";
        else if (ln.startsWith("+")) cls = "text-ok bg-ok/5";
        else if (ln.startsWith("-")) cls = "text-bad bg-bad/5";
        else if (ln.startsWith("@@")) cls = "text-vio";
        return (
          <div key={i} className={`${cls} whitespace-pre`}>
            {ln || " "}
          </div>
        );
      })}
    </pre>
  );
}

/** Polls a loader on an interval; returns fresh data plus load/error state. */
export function usePoll<T>(
  loader: () => Promise<T>,
  intervalMs: number | null,
  deps: unknown[] = [],
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  // Keep the ref fresh without touching it during render (React compiler rule).
  useEffect(() => {
    loaderRef.current = loader;
  }, [loader]);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async () => {
      setLoading(true);
      try {
        const v = await loaderRef.current();
        if (!alive) return;
        setData(v);
        setError(null);
      } catch (e) {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
      if (alive && intervalMs) timer = setTimeout(run, intervalMs);
    };
    run();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, tick, ...deps]);

  return { data, error, loading, refresh: () => setTick((t) => t + 1) };
}
