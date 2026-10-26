"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import {
  api,
  asText,
  AuditEvent,
  Finding,
  Patch,
  SEVERITY_TONE,
  short,
  STATUS_META,
  TestRun,
  VERIFICATION_META,
} from "@/lib/api";
import {
  Badge,
  Card,
  Dot,
  EmptyState,
  ErrorState,
  KV,
  Loading,
  Progress,
  usePoll,
} from "@/components/ui";

const TERMINAL = ["completed", "completed_with_errors", "failed"];

export default function AuditWorkspace() {
  const { id } = useParams<{ id: string }>();
  const auditId = Number(id);

  const audit = usePoll(() => api.get<any>(`/api/audits/${auditId}`), 1200);
  const events = usePoll<AuditEvent[]>(
    () => api.get<any>(`/api/audits/${auditId}/events?limit=300`).then((r) => (Array.isArray(r) ? r : [])),
    1200,
    [auditId],
  );

  if (audit.loading) return <Loading label="opening audit workspace…" />;
  if (audit.error) return <ErrorState message={audit.error} onRetry={audit.refresh} />;
  const a = audit.data;
  if (!a) return <EmptyState title="Audit not found" />;

  const meta = STATUS_META[a.status] ?? { tone: "muted" as const, label: a.status };
  const running = !TERMINAL.includes(a.status);
  const findings: Finding[] = a.findings ?? [];
  const patches: Patch[] = a.patches ?? [];
  const runs: TestRun[] = a.test_runs ?? [];
  const duo = a.duo_availability ?? {};

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="font-mono text-lg font-semibold">Audit #{a.id}</h1>
            <Badge tone={meta.tone}>
              <Dot tone={meta.tone} pulse={running} /> {meta.label}
            </Badge>
            <Badge tone="muted">{a.mode}</Badge>
            <Badge tone={a.engine === "gitlab_duo_flow" ? "vio" : "info"}>{a.engine}</Badge>
          </div>
          <p className="mt-1 font-mono text-[12px] text-muted">
            {a.source_branch} → {a.target_branch} · {short(a.source_commit_sha)} ←{" "}
            {short(a.target_commit_sha)}
          </p>
        </div>
        <div className="w-64">
          <div className="mb-1 flex justify-between font-mono text-[11px] text-muted">
            <span>{a.stage}</span>
            <span>{a.progress_pct}%</span>
          </div>
          <Progress pct={a.progress_pct} tone={meta.tone} />
        </div>
      </header>

      {a.error_detail && (
        <ErrorState message={`audit failed: ${a.error_detail}`} onRetry={audit.refresh} />
      )}

      {duo.available === false && (
        <div className="rounded-lg border border-warn/40 bg-warn/10 px-4 py-3">
          <p className="font-mono text-[12px] text-warn">
            GitLab Duo Agent Platform unavailable — {String(duo.reason ?? "").slice(0, 160)}
          </p>
          <p className="mt-1 text-[11px] text-muted">
            This audit therefore ran on the local deterministic engine and is labelled as
            such everywhere. No Duo result is simulated or substituted.
          </p>
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-3">
        <Card
          title="Live event stream"
          subtitle="durable provenance trail — survives restarts"
          pad={false}
          className="xl:col-span-2"
          right={<Badge tone={running ? "info" : "muted"}>{(events.data ?? []).length} events</Badge>}
        >
          <ul className="max-h-[420px] divide-y divide-line overflow-auto font-mono text-[11.5px]">
            {(events.data ?? []).slice().reverse().map((e) => (
              <li key={e.seq} className="row-in flex gap-3 px-4 py-1.5">
                <span className="w-9 shrink-0 text-muted">#{e.seq}</span>
                <span className="w-40 shrink-0 truncate text-vio">{e.agent_role}</span>
                <span className="w-36 shrink-0 truncate text-accent">{e.event_type}</span>
                <span className="min-w-0 flex-1 text-fg/90">{e.message}</span>
              </li>
            ))}
          </ul>
        </Card>

        <div className="space-y-5">
          <Card title="Session">
            <KV k="status" v={<Badge tone={meta.tone}>{meta.label}</Badge>} mono={false} />
            <KV k="stage" v={a.stage} />
            <KV k="engine" v={a.engine} />
            <KV k="source sha" v={short(a.source_commit_sha)} />
            <KV k="target sha" v={short(a.target_commit_sha)} />
            <KV k="findings" v={findings.length} />
            <KV k="patches" v={patches.length} />
            <KV k="test runs" v={runs.length} />
          </Card>
          <Card title="Navigation">
            <div className="flex flex-col gap-2">
              <Link href={`/findings?audit=${a.id}`} className="rounded border border-line px-3 py-2 font-mono text-[12px] text-accent hover:bg-panel2">
                → findings explorer ({findings.length})
              </Link>
              <Link href={`/verifications?audit=${a.id}`} className="rounded border border-line px-3 py-2 font-mono text-[12px] text-accent hover:bg-panel2">
                → verification results ({runs.length})
              </Link>
              {patches[0] && (
                <Link href={`/patches/${patches[0].id}`} className="rounded border border-line px-3 py-2 font-mono text-[12px] text-accent hover:bg-panel2">
                  → patch review {patches[0].public_id}
                </Link>
              )}
              {TERMINAL.includes(a.status) && (
                <Link href={`/audits/${a.id}/report`} className="rounded border border-ok/40 px-3 py-2 font-mono text-[12px] text-ok hover:bg-ok/10">
                  → final report
                </Link>
              )}
            </div>
          </Card>
        </div>
      </div>

      <div className="grid gap-5 xl:grid-cols-2">
        <Card title="Findings produced" pad={false}>
          {findings.length === 0 ? (
            <div className="p-4">
              <EmptyState title="No findings yet" hint="The analysis stage may still be running." />
            </div>
          ) : (
            <ul className="divide-y divide-line">
              {findings.map((f) => (
                <li key={String(f.id)} className="px-4 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-[12.5px] text-fg">
                      {f.public_id} · {f.rule_id}
                    </span>
                    <div className="flex gap-1.5">
                      <Badge tone={SEVERITY_TONE[f.severity] ?? "muted"}>{f.severity}</Badge>
                      <Badge tone={VERIFICATION_META[f.verification_status] ?? "muted"}>
                        {f.verification_status}
                      </Badge>
                    </div>
                  </div>
                  <p className="mt-1 truncate text-[11.5px] text-muted">{f.title}</p>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Executed test runs" pad={false}>
          {runs.length === 0 ? (
            <div className="p-4">
              <EmptyState title="Nothing executed yet" />
            </div>
          ) : (
            <ul className="divide-y divide-line font-mono text-[11.5px]">
              {runs.map((r) => (
                <li key={r.id} className="flex items-center justify-between gap-2 px-4 py-2">
                  <span className="truncate text-fg">{r.test_type}</span>
                  <span className="text-muted">{r.revision_label}</span>
                  <Badge tone={r.execution_status === "passed" ? "ok" : r.execution_status === "failed" ? "bad" : "muted"}>
                    exit {r.exit_code}
                  </Badge>
                  <span className="text-muted">
                    {r.tests_passed}/{r.tests_total} pass
                  </span>
                  <span className="text-muted">{r.isolation_level}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {a.report && (
        <Card
          title="Report summary"
          subtitle={asText((a.report as any).outcome?.label ?? (a.report as any).outcome)}
        >
          {/* The raw report column nests outcome as {label, explanation, reasons};
              rendering it directly crashes React. Always coerce. */}
          <p className="text-[12.5px] leading-5 text-fg/90">
            {asText((a.report as any).outcome?.explanation)}
          </p>
          <ul className="mt-2 space-y-1 text-[11.5px] text-muted">
            {(((a.report as any).outcome?.reasons ?? []) as string[]).map((r, i) => (
              <li key={i}>• {r}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
