"use client";

import { useParams } from "next/navigation";
import { api, asText, short } from "@/lib/api";
import { Badge, Card, EmptyState, ErrorState, KV, Loading, Pre, usePoll } from "@/components/ui";

export default function ReportPage() {
  const { id } = useParams<{ id: string }>();
  const report = usePoll(() => api.get<any>(`/api/audits/${id}/report`), 5000, [id]);

  if (report.loading) return <Loading label="loading final report…" />;
  if (report.error) return <ErrorState message={report.error} onRetry={report.refresh} />;
  const r = report.data;
  if (!r) return <EmptyState title="No report yet" hint="The audit must reach a terminal state." />;

  const body = r.report ?? {};
  const counts = body.counts ?? {};
  const list = (key: string) => (Array.isArray(body[key]) ? body[key] : []);

  return (
    <div className="space-y-5">
      <header>
        <div className="flex items-center gap-2">
          <h1 className="font-mono text-lg font-semibold">Final report</h1>
          <Badge tone="ok">{r.outcome}</Badge>
        </div>
        <p className="mt-1 max-w-3xl text-[12px] leading-5 text-muted">{asText(r.outcome_explanation)}</p>
      </header>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="Revisions audited" className="xl:col-span-1">
          <KV k="source (defective)" v={short(body.change?.source_commit_sha)} />
          <KV k="target (reference)" v={short(body.change?.target_commit_sha)} />
          <KV k="engine" v={body.audit?.engine ?? "—"} />
          <KV k="report id" v={r.public_id} />
          <KV k="created" v={String(r.created_at ?? "").slice(0, 19)} />
          <div className="mt-3 grid grid-cols-2 gap-2 font-mono text-[12px]">
            {Object.entries(counts as Record<string, number>).map(([k, v]) => (
              <div key={k} className="rounded border border-line bg-panel2 px-2 py-1.5">
                <div className="text-[10px] text-muted">{k}</div>
                <div className="text-base">{String(v)}</div>
              </div>
            ))}
          </div>
        </Card>

        <Card title="Remaining risks" className="xl:col-span-1">
          {list("remaining_risks").length === 0 ? (
            <p className="text-[12px] text-muted">none recorded</p>
          ) : (
            <ul className="space-y-2 text-[12px] leading-5 text-fg/90">
              {list("remaining_risks").map((x: string, i: number) => (
                <li key={i} className="flex gap-2">
                  <span className="text-warn">▸</span>
                  <span>{x}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Limitations the report states about itself" className="xl:col-span-1">
          <ul className="space-y-2 text-[12px] leading-5 text-muted">
            {list("limitations").map((x: string, i: number) => (
              <li key={i} className="flex gap-2">
                <span className="text-bad">✕</span>
                <span>{x}</span>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card title="Summary">
        <Pre text={asText(r.summary) || asText((r as any).report?.outcome?.explanation)} maxHeight={420} />
      </Card>

      <div className="grid gap-5 xl:grid-cols-2">
        <Card title="Recommended next steps">
          <ul className="space-y-1.5 font-mono text-[12px] text-fg/90">
            {list("recommended_next_steps").map((x: string, i: number) => (
              <li key={i}>→ {x}</li>
            ))}
            {list("recommended_next_steps").length === 0 && (
              <li className="text-muted">none recorded</li>
            )}
          </ul>
        </Card>
        <Card title="Verification evidence embedded in the report">
          <Pre text={JSON.stringify(body.regression_results ?? body.test_runs ?? {}, null, 2)} maxHeight={300} />
        </Card>
      </div>
    </div>
  );
}
