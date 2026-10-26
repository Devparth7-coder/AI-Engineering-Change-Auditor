"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { api, Health } from "@/lib/api";
import { Badge, Dot } from "@/components/ui";

const NAV = [
  { href: "/", label: "Dashboard", hint: "capability + portfolio" },
  { href: "/projects", label: "Project Explorer", hint: "pick an MR" },
  { href: "/history", label: "Audit History", hint: "past audits" },
  { href: "/findings", label: "Findings Explorer", hint: "evidence" },
  { href: "/verifications", label: "Verification Results", hint: "executed runs" },
  { href: "/settings", label: "Settings", hint: "config + Duo status" },
];

export default function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .get<Health>("/api/health")
        .then((h) => alive && setHealth(h))
        .catch(() => alive && setHealth(null));
    load();
    const t = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const isolation = (health?.isolation as any)?.selected_isolation_level;
  const duo = (health?.duo as any)?.available;

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-line bg-panel">
        <div className="border-b border-line px-4 py-4">
          <div className="font-mono text-[13px] font-bold tracking-tight text-fg">
            CHANGE<span className="text-accent">AUDITOR</span>
          </div>
          <div className="mt-1 text-[10px] uppercase tracking-[0.16em] text-muted">
            evidence · reproduction · verification
          </div>
        </div>
        <nav className="flex-1 space-y-0.5 p-2">
          {NAV.map((n) => {
            const active =
              n.href === "/" ? pathname === "/" : pathname.startsWith(n.href);
            return (
              <Link
                key={n.href}
                href={n.href}
                className={`block rounded px-3 py-2 transition-colors ${
                  active
                    ? "bg-panel2 text-fg shadow-[inset_2px_0_0_0_var(--color-accent)]"
                    : "text-muted hover:bg-panel2/60 hover:text-fg"
                }`}
              >
                <div className="text-[12.5px] font-medium">{n.label}</div>
                <div className="text-[10px] text-muted/80">{n.hint}</div>
              </Link>
            );
          })}
        </nav>
        <div className="space-y-1.5 border-t border-line px-4 py-3 text-[10.5px] font-mono">
          <div className="flex items-center justify-between">
            <span className="text-muted">api</span>
            <Badge tone={health ? (health.status === "ok" ? "ok" : "warn") : "bad"}>
              {health ? health.status : "down"}
            </Badge>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted">isolation</span>
            <Badge tone={isolation === "container" ? "ok" : isolation ? "warn" : "muted"}>
              {isolation ?? "—"}
            </Badge>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted">duo</span>
            <Badge tone={duo ? "ok" : "muted"}>{duo ? "available" : "unavailable"}</Badge>
          </div>
          {health?.demo?.status && (
            <div className="flex items-center justify-between">
              <span className="text-muted">demo fixture</span>
              <Badge tone={health.demo.usable ? "ok" : "bad"}>
                {String(health.demo.status)}
              </Badge>
            </div>
          )}
        </div>
      </aside>

      <main className="min-w-0 flex-1">
        <div className="flex items-center justify-between border-b border-line bg-panel/60 px-6 py-2.5">
          <div className="flex items-center gap-2 font-mono text-[11px] text-muted">
            <Dot tone="ok" pulse />
            <span>live · polling the backend, never a simulation</span>
          </div>
          <div className="font-mono text-[11px] text-muted">
            {health ? `v${health.version} · ${health.environment}` : "connecting…"}
          </div>
        </div>
        <div className="p-6">{children}</div>
      </main>
    </div>
  );
}
