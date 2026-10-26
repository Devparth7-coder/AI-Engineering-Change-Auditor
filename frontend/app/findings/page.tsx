"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api, Finding, SEVERITY_TONE, unwrap, VERIFICATION_META } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  KV,
  Loading,
  Pre,
  usePoll,
} from "@/components/ui";

function Explorer() {
  const params = useSearchParams();
  const auditId = params.get("audit");
  const [selected, setSelected] = useState<string | null>(null);
  const [confirmRun, setConfirmRun] = useState(false);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string | null>(null);

  const list = usePoll<{ findings: Finding[]; audits: any[] }>(
    async () => {
      if (auditId) {
        const f = await api.get<any>(`/api/audits/${auditId}/findings`);
        return { findings: unwrap<Finding>(f, "findings"), audits: [] };
      }
      const audits = unwrap<any>(await api.get<any>("/api/audits"), "audits");
      const all: Finding[] = [];
      for (const a of audits.slice(0, 6)) {
        const f = await api.get<any>(`/api/audits/${a.id}/findings`).catch(() => null);
        if (f) all.push(...unwrap<Finding>(f, "findings"));
      }
      return { findings: all, audits };
    },
    4000,
    [auditId],
  );

  const findings = list.data?.findings ?? [];
  const current =
    findings.find((f) => String(f.id) === selected) ??
    findings.find((f) => f.public_id === selected) ??
    findings[0] ??
    null;

  const detail = usePoll<Finding | null>(
    async () => (current ? api.get<Finding>(`/api/findings/${current.id}`) : null),
    3000,
    [current?.id],
  );

  const runRepro = async () => {
    if (!current) return;
    setRunning(true);
    setRunMsg(null);
    try {
      const res = await api.post<any>(`/api/findings/${current.id}/tests`);
      const run = res?.test_run ?? res;
      setRunMsg(
        `exit=${run?.exit_code} · ${run?.tests_passed}/${run?.tests_total} passed · ` +
          `status=${run?.execution_status} · isolation=${run?.isolation_level}`,
      );
      detail.refresh();
      list.refresh();
    } catch (e: any) {
      setRunMsg(`refused or failed: ${e?.message}`);
    } finally {
      setRunning(false);
    }
  };

  if (list.loading) return <Loading label="loading findings…" />;
  if (list.error) return <ErrorState message={list.error} onRetry={list.refresh} />;

  const f = detail.data ?? current;
  const inv = (f?.investigation ?? {}) as any;

  return (
    <div className="space-y-5">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="font-mono text-lg font-semibold">Findings Explorer</h1>
          <p className="text-[12px] text-muted">
            A hypothesis only becomes a finding once the repository corroborates it.
          </p>
        </div>
        <Badge tone="muted">{findings.length} finding(s)</Badge>
      </header>

      {findings.length === 0 ? (
        <EmptyState title="No findings" hint="Start an audit from Project Explorer." />
      ) : (
        <div className="grid gap-5 xl:grid-cols-3">
          <Card title="Findings" pad={false} className="xl:col-span-1">
            <ul className="max-h-[560px] divide-y divide-line overflow-auto">
              {findings.map((x) => (
                <li key={String(x.id)}>
                  <button
                    onClick={() => setSelected(String(x.id))}
                    className={`w-full px-4 py-3 text-left transition-colors ${
                      current && String(x.id) === String(current.id)
                        ? "bg-panel2"
                        : "hover:bg-panel2/60"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[12px] text-fg">{x.public_id}</span>
                      <Badge tone={SEVERITY_TONE[x.severity] ?? "muted"}>{x.severity}</Badge>
                    </div>
                    <div className="mt-1 line-clamp-2 text-[11.5px] text-muted">{x.title}</div>
                    <div className="mt-1.5 flex items-center gap-1.5">
                      <Badge tone={VERIFICATION_META[x.verification_status] ?? "muted"}>
                        {x.verification_status}
                      </Badge>
                      <span className="font-mono text-[10px] text-muted">{x.rule_id}</span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </Card>

          <div className="space-y-5 xl:col-span-2">
            {f && (
              <Card
                title={`${f.public_id} · ${f.title}`}
                subtitle={`${f.file_path}:${f.line_start}-${f.line_end} · ${f.symbol ?? ""}`}
                right={
                  <Button tone="warn" busy={running} onClick={() => setConfirmRun(true)}>
                    generate + run reproduction test
                  </Button>
                }
              >
                <div className="grid gap-4 md:grid-cols-2">
                  <div>
                    <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-ok">
                      expected behaviour
                    </div>
                    <p className="text-[12px] leading-5 text-fg/90">{f.expected_behavior}</p>
                  </div>
                  <div>
                    <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-bad">
                      suspected behaviour
                    </div>
                    <p className="text-[12px] leading-5 text-fg/90">{f.suspected_behavior}</p>
                  </div>
                </div>

                <div className="mt-4 grid grid-cols-3 gap-2 font-mono text-[11.5px]">
                  <div className="rounded border border-ok/30 bg-ok/5 px-3 py-2">
                    <div className="text-[10px] text-muted">corroborated</div>
                    <div className="text-lg text-ok">{(inv.corroborated ?? []).length}</div>
                  </div>
                  <div className="rounded border border-bad/30 bg-bad/5 px-3 py-2">
                    <div className="text-[10px] text-muted">refuted</div>
                    <div className="text-lg text-bad">{(inv.refuted ?? []).length}</div>
                  </div>
                  <div className="rounded border border-line bg-panel2 px-3 py-2">
                    <div className="text-[10px] text-muted">ratio</div>
                    <div className="text-lg">{inv.corroboration_ratio ?? "—"}</div>
                  </div>
                </div>

                {runMsg && (
                  <p className="mt-3 rounded border border-line bg-panel2 px-3 py-2 font-mono text-[11.5px] text-warn">
                    {runMsg}
                  </p>
                )}
              </Card>
            )}

            {f && (
              <Card title="Evidence" subtitle="each item declares how it can be independently checked" pad={false}>
                <ul className="divide-y divide-line">
                  {(f.evidence ?? []).map((e, i) => (
                    <li key={i} className="px-4 py-3">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-[11.5px] text-vio">
                          #{i} {e.kind}
                        </span>
                        <div className="flex gap-1.5">
                          <Badge tone="muted">check: {e.check ?? "source_lines"}</Badge>
                          <Badge tone="muted">
                            {e.file_path}:{e.line_start}-{e.line_end}
                          </Badge>
                        </div>
                      </div>
                      <p className="mt-1 text-[12px] text-fg/90">{e.description}</p>
                      {e.content && (
                        <div className="mt-2">
                          <Pre text={e.content} maxHeight={140} />
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </Card>
            )}

            {f && (
              <div className="grid gap-5 md:grid-cols-2">
                <Card title="Confirmed facts">
                  <ul className="space-y-1.5 text-[12px] leading-5 text-fg/90">
                    {(f.facts ?? []).map((x, i) => (
                      <li key={i} className="flex gap-2">
                        <span className="text-ok">✓</span>
                        <span>{x}</span>
                      </li>
                    ))}
                    {(f.facts ?? []).length === 0 && <li className="text-muted">none</li>}
                  </ul>
                </Card>
                <Card title="Hypotheses (explicitly NOT facts)">
                  <ul className="space-y-1.5 text-[12px] leading-5 text-muted">
                    {(f.hypotheses ?? []).map((x, i) => (
                      <li key={i} className="flex gap-2">
                        <span className="text-warn">?</span>
                        <span>{x}</span>
                      </li>
                    ))}
                    {(f.hypotheses ?? []).length === 0 && <li>none</li>}
                  </ul>
                  <div className="mt-3 border-t border-line pt-2">
                    <KV
                      k="confidence"
                      v={
                        <span className="text-muted">
                          {JSON.stringify((f.confidence ?? {}) as any).slice(0, 120)}
                        </span>
                      }
                    />
                    <p className="mt-1 text-[10.5px] text-muted">
                      Confidence is deliberately uncalibrated — no accuracy percentage is
                      invented anywhere in this product.
                    </p>
                  </div>
                </Card>
              </div>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmRun}
        title="Generate and EXECUTE a reproduction test?"
        tone="warn"
        confirmLabel="execute in isolation"
        body={
          <>
            <p>
              A test will be generated from the declared contract and run against the{" "}
              <span className="font-mono text-fg">original</span> revision inside an
              isolated workspace with restricted resources and (where the platform
              allows) no network.
            </p>
            <p className="mt-2 text-warn">
              Untrusted repository code will execute. The isolation tier in use is shown
              in Settings and is documented as NOT a VM-grade boundary.
            </p>
          </>
        }
        onCancel={() => setConfirmRun(false)}
        onConfirm={() => {
          setConfirmRun(false);
          runRepro();
        }}
      />
    </div>
  );
}

export default function FindingsPage() {
  return (
    <Suspense fallback={<Loading label="loading findings…" />}>
      <Explorer />
    </Suspense>
  );
}
