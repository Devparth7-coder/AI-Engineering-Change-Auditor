"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, MergeRequest, Project, unwrap } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Loading,
  usePoll,
} from "@/components/ui";

export default function ProjectExplorer() {
  const router = useRouter();
  const projects = usePoll(() => api.get<any>("/api/projects"), 10000);
  const [selected, setSelected] = useState<number | null>(null);
  const [starting, setStarting] = useState<MergeRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const list: Project[] = Array.isArray(projects.data) ? projects.data : [];
  const projectId = selected ?? list[0]?.id ?? null;
  const mrs = usePoll<MergeRequest[]>(
    async () =>
      projectId == null ? [] : unwrap<MergeRequest>(
        await api.get<any>(`/api/projects/${projectId}/merge-requests`),
        "merge_requests",
      ),
    10000,
    [projectId],
  );

  const startAudit = async () => {
    if (!starting || projectId == null) return;
    setBusy(true);
    setFormError(null);
    try {
      const created = await api.post<any>("/api/audits", {
        project_id: projectId,
        merge_request_iid: starting.iid,
        source_branch: starting.source_branch,
        target_branch: starting.target_branch,
        mode: projectId && list.find((p) => p.id === projectId)?.origin === "local_demo"
          ? "local_demo"
          : "gitlab",
        auto_start: true,
      });
      router.push(`/audits/${created.id}`);
    } catch (e: any) {
      setFormError(e?.message || String(e));
      setBusy(false);
    }
  };

  if (projects.loading) return <Loading label="listing projects…" />;
  if (projects.error) return <ErrorState message={projects.error} onRetry={projects.refresh} />;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="font-mono text-lg font-semibold">Project Explorer</h1>
        <p className="text-[12px] text-muted">
          Connect a project, read its merge requests, and start an audit. Demonstration
          repositories are labelled as such and never presented as live GitLab data.
        </p>
      </header>

      {list.length === 0 ? (
        <EmptyState
          title="No projects reachable"
          hint="Configure GITLAB_URL/GITLAB_TOKEN server-side, or enable the local demo fixture."
        />
      ) : (
        <div className="grid gap-5 xl:grid-cols-3">
          <Card title="Projects" pad={false} className="xl:col-span-1">
            <ul className="divide-y divide-line">
              {list.map((p) => (
                <li key={p.id}>
                  <button
                    onClick={() => setSelected(p.id)}
                    className={`w-full px-4 py-3 text-left transition-colors ${
                      projectId === p.id ? "bg-panel2" : "hover:bg-panel2/60"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-mono text-[12.5px] text-fg">{p.name}</span>
                      <Badge tone={p.origin === "local_demo" ? "warn" : "info"}>
                        {p.origin}
                      </Badge>
                    </div>
                    <div className="mt-0.5 truncate text-[11px] text-muted">
                      {p.path_with_namespace} · default {p.default_branch}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </Card>

          <Card
            title="Merge requests"
            subtitle={projectId != null ? `project #${projectId}` : undefined}
            pad={false}
            className="xl:col-span-2"
            right={
              mrs.loading ? <Badge tone="muted">loading…</Badge> : <Badge tone="muted">{(mrs.data ?? []).length} open</Badge>
            }
          >
            {mrs.error ? (
              <div className="p-4">
                <ErrorState message={mrs.error} onRetry={mrs.refresh} />
              </div>
            ) : (mrs.data ?? []).length === 0 ? (
              <div className="p-4">
                <EmptyState title="No merge requests discovered" />
              </div>
            ) : (
              <ul className="divide-y divide-line">
                {(mrs.data ?? []).map((m) => (
                  <li key={m.iid} className="flex items-center justify-between gap-3 px-4 py-3">
                    <div className="min-w-0">
                      <div className="truncate font-mono text-[12.5px] text-fg">
                        !{m.iid} {m.title}
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-muted">
                        {m.source_branch} → {m.target_branch}
                        {typeof m.changed_files === "number" && (
                          <span> · {m.changed_files} file(s) changed</span>
                        )}
                      </div>
                    </div>
                    <Button tone="info" onClick={() => setStarting(m)}>
                      start audit
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}

      <ConfirmDialog
        open={starting !== null}
        title="Start an audit of this merge request?"
        tone="info"
        confirmLabel="yes, start the audit"
        body={
          <>
            <p>
              The audit analyses the diff between{" "}
              <span className="font-mono text-fg">{starting?.source_branch}</span> and{" "}
              <span className="font-mono text-fg">{starting?.target_branch}</span>, runs
              generated tests, and may propose a patch.
            </p>
            <p className="mt-2">
              It will <span className="text-warn">never</span> push, merge, or deploy
              anything. Verification of any patch still requires your explicit approval
              later.
            </p>
            {formError && <p className="mt-2 font-mono text-bad">{formError}</p>}
          </>
        }
        onCancel={() => {
          setStarting(null);
          setFormError(null);
        }}
        onConfirm={startAudit}
      />
      {busy && (
        <div className="fixed bottom-4 right-4 rounded border border-accent/40 bg-panel px-4 py-2 font-mono text-[12px] text-accent">
          creating audit session…
        </div>
      )}
    </div>
  );
}
