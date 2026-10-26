"use client";

import Link from "next/link";
import { api, Audit, short, STATUS_META, timeAgo, unwrap } from "@/lib/api";
import { Badge, Card, EmptyState, ErrorState, Loading, Progress, usePoll } from "@/components/ui";

export default function History() {
  const audits = usePoll<Audit[]>(
    () => api.get<any>("/api/audits").then((r) => unwrap<Audit>(r, "audits")),
    6000,
  );

  if (audits.loading) return <Loading label="loading audit history…" />;
  if (audits.error) return <ErrorState message={audits.error} onRetry={audits.refresh} />;
  const list = audits.data ?? [];

  return (
    <div className="space-y-5">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="font-mono text-lg font-semibold">Audit History</h1>
          <p className="text-[12px] text-muted">
            Audits are durable: reopening after a restart restores findings, runs,
            decisions and the report.
          </p>
        </div>
        <Badge tone="muted">{list.length} session(s)</Badge>
      </header>

      {list.length === 0 ? (
        <EmptyState title="No audits recorded" hint="Start one from Project Explorer." />
      ) : (
        <Card pad={false}>
          <table className="w-full font-mono text-[11.5px]">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="px-4 py-2 font-normal">#</th>
                <th className="px-4 py-2 font-normal">merge request</th>
                <th className="px-4 py-2 font-normal">branches</th>
                <th className="px-4 py-2 font-normal">status</th>
                <th className="px-4 py-2 font-normal">engine</th>
                <th className="px-4 py-2 font-normal">findings</th>
                <th className="px-4 py-2 font-normal">runs</th>
                <th className="px-4 py-2 font-normal">created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {list.map((a) => {
                const meta = STATUS_META[a.status] ?? { tone: "muted" as const, label: a.status };
                return (
                  <tr key={a.id} className="hover:bg-panel2/50">
                    <td className="px-4 py-2.5">
                      <Link href={`/audits/${a.id}`} className="text-accent underline">
                        {a.id}
                      </Link>
                    </td>
                    <td className="max-w-[280px] truncate px-4 py-2.5 text-fg">
                      {a.merge_request_title || "—"}
                    </td>
                    <td className="px-4 py-2.5 text-muted">
                      {a.source_branch} → {a.target_branch}
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge tone={meta.tone}>{meta.label}</Badge>
                    </td>
                    <td className="px-4 py-2.5 text-muted">{a.engine}</td>
                    <td className="px-4 py-2.5">{a.findings_count ?? 0}</td>
                    <td className="px-4 py-2.5">{a.test_runs_count ?? 0}</td>
                    <td className="px-4 py-2.5 text-muted">{timeAgo(a.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}

      {list.length > 0 && (
        <div className="grid gap-5 md:grid-cols-3">
          {list.slice(0, 3).map((a) => (
            <Card key={a.id} title={`audit #${a.id}`} subtitle={short(a.source_commit_sha)}>
              <div className="mb-2 flex justify-between font-mono text-[11px] text-muted">
                <span>{a.stage}</span>
                <span>{a.progress_pct}%</span>
              </div>
              <Progress
                pct={a.progress_pct}
                tone={(STATUS_META[a.status] ?? { tone: "muted" as const }).tone}
              />
              <div className="mt-3 flex gap-2">
                <Link
                  href={`/audits/${a.id}`}
                  className="flex-1 rounded border border-line px-2 py-1.5 text-center font-mono text-[11px] text-accent hover:bg-panel2"
                >
                  workspace
                </Link>
                <Link
                  href={`/audits/${a.id}/report`}
                  className="flex-1 rounded border border-line px-2 py-1.5 text-center font-mono text-[11px] text-ok hover:bg-panel2"
                >
                  report
                </Link>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
