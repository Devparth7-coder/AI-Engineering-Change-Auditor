"use client";

import Link from "next/link";
import { api, Audit, STATUS_META, timeAgo, VERIFICATION_META } from "@/lib/api";
import {
  Badge,
  Card,
  Dot,
  EmptyState,
  ErrorState,
  Loading,
  Progress,
  Stat,
  usePoll,
} from "@/components/ui";

export default function Dashboard() {
  const { data, error, loading, refresh } = usePoll(
    () => api.get<any>("/api/dashboard"),
    4000,
  );

  if (loading) return <Loading label="reading portfolio from the database…" />;
  if (error) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return <EmptyState title="No dashboard data" />;

  const totals = data.totals ?? {};
  const runs = data.test_runs ?? {};
  const recent: Audit[] = data.recent_audits ?? [];
  const active = recent.filter(
    (a) => !["completed", "completed_with_errors", "failed"].includes(a.status),
  );

  return (
    <div className="space-y-5">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="font-mono text-lg font-semibold">Dashboard</h1>
          <p className="text-[12px] text-muted">
            Every number below is read from persisted rows — nothing is estimated.
          </p>
        </div>
        <Badge tone="muted">engine usage: {JSON.stringify(data.engine_usage ?? {})}</Badge>
      </header>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="audits" value={totals.audits ?? 0} />
        <Stat
          label="active"
          value={totals.active_audits ?? 0}
          tone={active.length ? "info" : "muted"}
        />
        <Stat label="findings" value={totals.findings ?? 0} tone="warn" />
        <Stat label="reproduced" value={totals.findings_reproduced ?? 0} tone="warn" />
        <Stat label="verified fixes" value={data.verified_fixes ?? 0} tone="ok" />
        <Stat
          label="pending approval"
          value={data.patches_pending_approval ?? 0}
          tone={data.patches_pending_approval ? "warn" : "muted"}
        />
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card
          title="Recent audits"
          subtitle="click an audit to open its workspace"
          className="xl:col-span-2"
          pad={false}
        >
          {recent.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No audits yet"
                hint="Open Project Explorer, pick a merge request, and start one."
              />
            </div>
          ) : (
            <ul className="divide-y divide-line">
              {recent.slice(0, 8).map((a) => {
                const meta = STATUS_META[a.status] ?? { tone: "muted" as const, label: a.status };
                return (
                  <li key={a.id}>
                    <Link
                      href={`/audits/${a.id}`}
                      className="block px-4 py-3 transition-colors hover:bg-panel2/60"
                    >
                      <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate font-mono text-[12.5px] text-fg">
                            #{a.id} {a.merge_request_title || a.source_branch}
                          </div>
                          <div className="mt-0.5 truncate text-[11px] text-muted">
                            {a.source_branch} → {a.target_branch} · engine{" "}
                            <span className="font-mono">{a.engine}</span> · {timeAgo(a.created_at)}
                          </div>
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          <Badge tone={meta.tone}>{meta.label}</Badge>
                          <span className="font-mono text-[11px] text-muted">
                            {a.progress_pct}%
                          </span>
                        </div>
                      </div>
                      <div className="mt-2">
                        <Progress pct={a.progress_pct} tone={meta.tone} />
                      </div>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>

        <div className="space-y-5">
          <Card title="Execution totals" subtitle="real pytest runs, persisted">
            <div className="grid grid-cols-2 gap-2 font-mono text-[12px]">
              <div className="rounded border border-line bg-panel2 px-3 py-2">
                <div className="text-[10px] text-muted">runs</div>
                <div className="text-lg">{runs.runs ?? 0}</div>
              </div>
              <div className="rounded border border-line bg-panel2 px-3 py-2">
                <div className="text-[10px] text-muted">assertions</div>
                <div className="text-lg">{runs.tests_total ?? 0}</div>
              </div>
              <div className="rounded border border-ok/30 bg-ok/5 px-3 py-2">
                <div className="text-[10px] text-muted">passed</div>
                <div className="text-lg text-ok">{runs.tests_passed ?? 0}</div>
              </div>
              <div className="rounded border border-bad/30 bg-bad/5 px-3 py-2">
                <div className="text-[10px] text-muted">failed</div>
                <div className="text-lg text-bad">{runs.tests_failed ?? 0}</div>
              </div>
            </div>
            <div className="mt-3 space-y-1">
              {Object.entries((runs.by_status ?? {}) as Record<string, number>).map(([k, v]) => (
                <div key={k} className="flex items-center justify-between text-[11px]">
                  <span className="flex items-center gap-1.5 font-mono text-muted">
                    <Dot tone={VERIFICATION_META[k] ?? "muted"} />
                    {k}
                  </span>
                  <span className="font-mono text-fg">{v}</span>
                </div>
              ))}
            </div>
          </Card>

          <Card title="Severity mix">
            <div className="space-y-1.5">
              {Object.entries((data.findings_by_severity ?? {}) as Record<string, number>).map(
                ([k, v]) => (
                  <div key={k} className="flex items-center gap-2">
                    <span className="w-20 font-mono text-[11px] text-muted">{k}</span>
                    <div className="h-2 flex-1 overflow-hidden rounded bg-panel2">
                      <div
                        className={`h-full ${
                          k === "critical" || k === "high"
                            ? "bg-bad"
                            : k === "medium"
                              ? "bg-warn"
                              : "bg-muted"
                        }`}
                        style={{ width: `${Math.min(100, v * 25)}%` }}
                      />
                    </div>
                    <span className="w-6 text-right font-mono text-[11px]">{v}</span>
                  </div>
                ),
              )}
              {Object.keys(data.findings_by_severity ?? {}).length === 0 && (
                <p className="text-[11px] text-muted">no findings recorded yet</p>
              )}
            </div>
          </Card>
        </div>
      </div>

      <Card title="Live event stream" subtitle="the durable provenance trail across all audits" pad={false}>
        {(data.recent_events ?? []).length === 0 ? (
          <div className="p-4">
            <EmptyState title="No events yet" />
          </div>
        ) : (
          <ul className="max-h-64 divide-y divide-line overflow-auto font-mono text-[11.5px]">
            {(data.recent_events ?? []).slice(0, 30).map((e: any) => (
              <li key={`${e.id}-${e.seq}`} className="row-in flex gap-3 px-4 py-1.5">
                <span className="w-10 shrink-0 text-muted">#{e.seq}</span>
                <span className="w-40 shrink-0 truncate text-vio">{e.agent_role}</span>
                <span className="w-36 shrink-0 truncate text-accent">{e.event_type}</span>
                <span className="min-w-0 flex-1 truncate text-fg/90">{e.message}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
