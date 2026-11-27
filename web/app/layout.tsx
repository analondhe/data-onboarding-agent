import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Data Onboarding Agent",
  description: "Turn messy customer files into clean, tested tables",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
            <Link href="/" className="text-lg font-semibold">
              Data Onboarding Agent
            </Link>
            <nav className="flex items-center gap-6 text-sm">
              <Link href="/" className="text-slate-700 hover:text-indigo-700">
                New run
              </Link>
              <Link href="/sources" className="text-slate-700 hover:text-indigo-700">
                Sources
              </Link>
              <span className="text-slate-500">Acme Cloud migrations</span>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
