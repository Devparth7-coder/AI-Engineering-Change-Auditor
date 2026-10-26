import type { Metadata } from "next";
import "./globals.css";
import Shell from "@/components/Shell";

export const metadata: Metadata = {
  title: "AI Engineering Change Auditor",
  description:
    "Evidence-driven audit of software changes: findings backed by repository " +
    "evidence, defects reproduced by executing tests, fixes verified by real " +
    "execution. A generated patch is never reported as a fix.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full bg-bg text-fg">
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
