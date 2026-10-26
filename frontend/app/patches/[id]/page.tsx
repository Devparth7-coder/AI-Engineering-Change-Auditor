"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { api, Patch, VERIFICATION_META } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  DiffView,
  ErrorState,
  KV,
  Loading,
  Pre,
  usePoll,
} from "@/components/ui";

export default function PatchReview() {
  const { id } = useParams<{ id: string }>();
  const patch = usePoll<Patch>(() => api.get<Patch>(`/api/patches/${id}`), 3000, [id]);
  const [confirm, setConfirm] = useState<"approve" | "reject" | "verify" | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("Reviewed against the evidence and reproduction run.");
  const [message, setMessage] = useState<string | null>(null);
  const [verification, setVerification] = useState<any | null>(null);

  const act = async (kind: "approve" | "reject" | "verify") => {
    setBusy(true);
    setMessage(null);
    try {
      if (kind === "verify") {
        const res = await api.post<any>(`/api/patches/${id}/verify`);
        setVerification(res);
        setMessage(`verification outcome: ${res.outcome}`);
      } else {
        const res = await api.post<any>(`/api/patches/${id}/${kind}`, {
          decided_by: "human-reviewer",
          note,
        });
        setMessage(`${kind} recorded for ${(res as any).public_id}`);
      }
      patch.refresh();
    } catch (e: any) {
      setMessage(`refused: ${e?.message}`);
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  if (patch.loading) return <Loading label="loading patch proposal…" />;
  if (patch.error) return <ErrorState message={patch.error} onRetry={patch.refresh} />;
  const p = patch.data;
  if (!p) return <ErrorState message="patch not found" />;

  const approved = p.approval_status === "approved";
  const rejected = p.approval_status === "rejected";
  const runs: any[] = verification?.runs ?? p.verification_detail?.runs ?? [];

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="font-mono text-lg font-semibold">Patch {p.public_id}</h1>
            <Badge
              tone={approved ? "ok" : rejected ? "bad" : "warn"}
            >
              {p.approval_status}
            </Badge>
            <Badge tone={VERIFICATION_META[p.verification_outcome] ?? "muted"}>
              {p.verification_outcome}
            </Badge>
          </div>
          <p className="mt-1 font-mono text-[12px] text-muted">{p.file_path}</p>
        </div>
        <div className="flex items-center gap-2">
          {!approved && !rejected && (
            <>
              <Button tone="ok" busy={busy} onClick={() => setConfirm("approve")}>
                approve
              </Button>
              <Button tone="bad" busy={busy} onClick={() => setConfirm("reject")}>
                reject
              </Button>
            </>
          )}
          {approved && p.verification_outcome === "not_attempted" && (
            <Button tone="vio" busy={busy} onClick={() => setConfirm("verify")}>
              verify by execution
            </Button>
          )}
        </div>
      </header>

      {message && (
        <div className="rounded border border-line bg-panel2 px-4 py-2 font-mono text-[12px] text-warn">
          {message}
        </div>
      )}

      <Card title="Proposed correction" subtitle="original and proposal are stored separately; nothing is applied to any real branch">
        <DiffView text={p.patch_text} />
        <div className="mt-4">
          <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-muted">
            why this change
          </div>
          <p className="text-[12.5px] leading-5 text-fg/90">{p.explanation}</p>
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="Side-effect analysis">
          <ul className="space-y-2 text-[12px] leading-5 text-fg/90">
            {(p.side_effects ?? []).map((s, i) => (
              <li key={i} className="flex gap-2">
                <span className="text-warn">▸</span>
                <span>{s}</span>
              </li>
            ))}
            {(p.side_effects ?? []).length === 0 && <li className="text-muted">none recorded</li>}
          </ul>
        </Card>
        <Card title="Decision record">
          <KV k="status" v={p.approval_status} />
          <KV k="decided by" v={p.decided_by ?? "—"} />
          <KV k="note" v={p.decision_note ?? "—"} mono={false} />
          <KV k="decided at" v={String(p.decided_at ?? "—").slice(0, 19)} />
          <KV k="related tests" v={(p.related_tests ?? []).join(", ") || "—"} />
          <div className="mt-3">
            <label className="text-[10px] uppercase tracking-[0.14em] text-muted">
              your decision note
            </label>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={3}
              className="mt-1 w-full rounded border border-line bg-bg px-2 py-1.5 font-mono text-[12px] text-fg outline-none focus:border-accent/60"
            />
          </div>
        </Card>
        <Card title="Verification scope">
          <p className="text-[12px] leading-5 text-muted">
            {p.verification_detail?.scope_statement ||
              verification?.scope_statement ||
              "Not verified yet. Verification executes four separate pytest runs in an isolated workspace and refuses to run until this patch is approved by a human."}
          </p>
        </Card>
      </div>

      {runs.length > 0 && (
        <Card title="Executed verification runs" subtitle="four separate pytest invocations, each in its own isolated workspace" pad={false}>
          <table className="w-full font-mono text-[11.5px]">
            <thead>
              <tr className="border-b border-line text-left text-muted">
                <th className="px-4 py-2 font-normal">run</th>
                <th className="px-4 py-2 font-normal">status</th>
                <th className="px-4 py-2 font-normal">exit</th>
                <th className="px-4 py-2 font-normal">passed/total</th>
                <th className="px-4 py-2 font-normal">patch applied</th>
                <th className="px-4 py-2 font-normal">isolation</th>
                <th className="px-4 py-2 font-normal">ms</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {runs.map((r: any, i: number) => (
                <tr key={i}>
                  <td className="px-4 py-2 text-fg">{r.label}</td>
                  <td className="px-4 py-2">
                    <Badge tone={r.execution_status === "passed" ? "ok" : "bad"}>
                      {r.execution_status}
                    </Badge>
                  </td>
                  <td className="px-4 py-2">{r.exit_code}</td>
                  <td className="px-4 py-2">
                    {r.tests_passed}/{r.tests_total}
                  </td>
                  <td className="px-4 py-2">{String(r.patch_applied)}</td>
                  <td className="px-4 py-2 text-muted">{r.isolation_level}</td>
                  <td className="px-4 py-2 text-muted">{r.duration_ms}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(verification?.remaining_concerns ?? p.verification_detail?.remaining_concerns ?? []).length > 0 && (
            <div className="border-t border-line px-4 py-3">
              <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-muted">
                what this does NOT prove
              </div>
              <ul className="space-y-1 text-[11.5px] text-muted">
                {(verification?.remaining_concerns ?? p.verification_detail?.remaining_concerns ?? []).map(
                  (c: string, i: number) => (
                    <li key={i}>✕ {c}</li>
                  ),
                )}
              </ul>
            </div>
          )}
        </Card>
      )}

      <div className="grid gap-5 md:grid-cols-2">
        <Card title="Original code (source revision)">
          <Pre text={p.original_code} maxHeight={360} />
        </Card>
        <Card title="Proposed code (not applied anywhere real)">
          <Pre text={p.proposed_code} maxHeight={360} />
        </Card>
      </div>

      <ConfirmDialog
        open={confirm !== null}
        title={
          confirm === "approve"
            ? "Approve this patch for isolated verification?"
            : confirm === "reject"
              ? "Reject this patch?"
              : "Execute verification now?"
        }
        tone={confirm === "approve" ? "ok" : confirm === "reject" ? "bad" : "vio"}
        confirmLabel={confirm === "approve" ? "yes, approve" : confirm === "reject" ? "yes, reject" : "run verification now"}
        body={
          confirm === "verify" ? (
            <>
              <p>
                Four pytest runs will execute in throwaway isolated workspaces: baseline
                reproduction, baseline regression, post-fix reproduction, post-fix
                regression.
              </p>
              <p className="mt-2">
                Nothing is pushed, merged or deployed. The patch is applied only inside
                the audit workspace.
              </p>
            </>
          ) : (
            <p>
              Your decision is recorded with your identity and timestamp, and becomes part
              of the immutable audit trail.
            </p>
          )
        }
        onCancel={() => setConfirm(null)}
        onConfirm={() => confirm && act(confirm)}
      />
    </div>
  );
}
