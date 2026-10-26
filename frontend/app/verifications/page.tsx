"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { api, short, TestRun, unwrap } from "@/lib/api";
import { Badge, Card, EmptyState, ErrorState, Loading, Pre, usePoll } from "@/components/ui";

function Results() {
  const params = useSearchParams();
  const auditId = params.get("audit");

  const runs = usePoll<TestRun[]>(
    async () => {
      if (auditId) {
        const r = await api.get<any>(`/api/audits/${auditId}/test-runs`);
        return unwrap<TestRun>(r, "test_runs");
      }
      const audits = unwrap<any>(await api.get<any>("/api/audits"), "audits");
      const all: TestRun[] = [];
      for (const a of audits.slice(0, 5)) {
        const r = await api.get<any>(`/api/audits/${a.id}/test-runs`).catch(() => null);
        if (r) all.push(...unwrap<TestRun>(r, "test_runs"));
      }
      return all;
    },
    4000,
    [auditId],
  );

  if (runs.loading) return <Loading label="loading executed runs…" />;
  if (runs.error) return <ErrorState message={runs.error} onRetry={runs.refresh} />;
  const list = runs.data ?? [];

  return (
    <div className="space-y-5">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="font-mono text-lg font-semibold">Verification Results</h1>
          <p className="text-[12px] text-muted">
            Every row is a pytest process that really ran. Exit codes are real, not
            inferred.
          </p>
        </div>
        <Badge tone="muted">{list.length} run(s)</Badge>
      </header>

      {list.length === 0 ? (
        <EmptyState title="Nothing has been executed yet" />
      ) : (
        <Card pad={false}>
          <table className="w-full font-mono text-[11.5px]">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="px-4 py-2 font-normal">id</th>
                <th className="px-4 py-2 font-normal">type</th>
                <th className="px-4 py-2 font-normal">revision</th>
                <th className="px-4 py-2 font-normal">status</th>
                <th className="px-4 py-2 font-normal">exit</th>
                <th className="px-4 py-2 font-normal">pass/total</th>
                <th className="px-4 py-2 font-normal">intended ran</th>
                <th className="px-4 py-2 font-normal">isolation</th>
                <th className="px-4 py-2 font-normal">net off</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {list.map((r) => (
                <tr key={r.id} className="hover:bg-panel2/50">
                  <td className="px-4 py-2 text-fg">{r.public_id}</td>
                  <td className="px-4 py-2 text-vio">{r.test_type}</td>
                  <td className="px-4 py-2 text-muted">
                    {short(r.code_revision)} ({r.revision_label})
                  </td>
                  <td className="px-4 py-2">
                    <Badge tone={r.execution_status === "passed" ? "ok" : r.execution_status === "failed" ? "bad" : "muted"}>
                      {r.execution_status}
                    </Badge>
                  </td>
                  <td className="px-4 py-2">{r.exit_code}</td>
                  <td className="px-4 py-2">
                    {r.tests_passed}/{r.tests_total}
                  </td>
                  <td className="px-4 py-2">{String(r.intended_tests_executed)}</td>
                  <td className="px-4 py-2 text-muted">{r.isolation_level}</td>
                  <td className="px-4 py-2 text-muted">{String(r.network_disabled)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {list.length > 0 && (
        <div className="grid gap-5 xl:grid-cols-2">
          <Card title="Sanitised output of the selected run" subtitle={list[0].test_file_name}>
            <Pre text={list[0].sanitized_output ?? ""} maxHeight={420} />
          </Card>
          <Card title="The exact test source that executed" subtitle="persisted so a reviewer can confirm intent">
            <Pre text={list[0].test_source ?? "(not stored for this run)"} maxHeight={420} />
          </Card>
        </div>
      )}

      <p className="text-[11px] text-muted">
        Runs are bound to commit SHAs ({list[0] ? short(list[0].code_revision) : "—"}), so a
        result can always be replayed against the exact revision that produced it.{" "}
        <Link href="/history" className="text-accent underline">
          audit history →
        </Link>
      </p>
    </div>
  );
}

export default function VerificationsPage() {
  return (
    <Suspense fallback={<Loading />}>
      <Results />
    </Suspense>
  );
}
