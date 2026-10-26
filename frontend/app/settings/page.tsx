"use client";

import { api } from "@/lib/api";
import { Badge, Card, ErrorState, KV, Loading, Pre, usePoll } from "@/components/ui";

export default function SettingsPage() {
  const settings = usePoll(() => api.get<any>("/api/settings"), 10000);
  const duo = usePoll(() => api.get<any>("/api/duo/availability"), 10000);

  if (settings.loading) return <Loading label="loading configuration…" />;
  if (settings.error) return <ErrorState message={settings.error} onRetry={settings.refresh} />;
  const s = settings.data ?? {};
  const d = duo.data ?? {};

  return (
    <div className="space-y-5">
      <header>
        <h1 className="font-mono text-lg font-semibold">Settings</h1>
        <p className="text-[12px] text-muted">
          Server-side configuration. Secrets are never returned by the API and are only
          ever read from the process environment.
        </p>
      </header>

      <div className="grid gap-5 xl:grid-cols-2">
        <Card title="GitLab Duo Agent Platform" subtitle="mandatory integration — status is probed, never assumed">
          <KV k="available" v={<Badge tone={d.available ? "ok" : "bad"}>{String(d.available)}</Badge>} mono={false} />
          <KV k="flow definition" v={d.flow_definition ?? "—"} />
          <KV k="flow yaml present" v={String(d.flow_yaml_present ?? false)} />
          <KV k="fallback engine" v={d.engine_used_when_unavailable ?? "—"} />
          <div className="mt-3 rounded border border-warn/40 bg-warn/10 px-3 py-2">
            <div className="text-[10px] uppercase tracking-[0.14em] text-warn">
              missing prerequisite
            </div>
            <p className="mt-1 text-[11.5px] leading-5 text-fg/90">
              {d.missing_prerequisite ?? "none — the platform is reachable"}
            </p>
          </div>
          <div className="mt-3">
            <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-muted">checks</div>
            <Pre text={JSON.stringify(d.checks ?? [], null, 2)} maxHeight={140} />
          </div>
        </Card>

        <Card title="Execution isolation" subtitle="a git worktree alone is not a security boundary">
          <Pre text={JSON.stringify(s.isolation ?? {}, null, 2)} maxHeight={300} />
        </Card>

        <Card title="Demonstration fixture">
          <KV k="status" v={<Badge tone={s.demo_repository?.usable ? "ok" : "bad"}>{s.demo_repository?.status ?? "—"}</Badge>} mono={false} />
          <KV k="path" v={s.demo_repository?.path ?? "—"} />
          <KV k="main sha" v={s.demo_repository?.main_sha ?? "—"} />
          <KV k="source sha" v={s.demo_repository?.source_sha ?? "—"} />
          <KV k="branches" v={(s.demo_repository?.branches ?? []).join(", ")} />
          <KV k="source branch" v={s.demo_repository?.source_branch ?? "—"} />
          <KV k="target branch" v={s.demo_repository?.target_branch ?? "—"} />
          <p className="mt-2 text-[11px] text-muted">{s.demo_repository?.detail ?? ""}</p>
        </Card>

        <Card title="Redacted configuration" subtitle="secret values are never returned">
          <Pre text={JSON.stringify(s.config ?? {}, null, 2)} maxHeight={420} />
          <ul className="mt-3 space-y-1 text-[11px] text-muted">
            {(s.notes ?? []).map((n: string, i: number) => (
              <li key={i}>• {n}</li>
            ))}
          </ul>
        </Card>
      </div>
    </div>
  );
}
