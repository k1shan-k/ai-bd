import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "SponsorFlow CRM",
  description: "Policy-controlled sponsorship outreach operations",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <Link href="/" className="brand"><span>SF</span> SponsorFlow</Link>
          <nav>
            <Link href="/">Overview</Link>
            <Link href="/leads">Pipeline</Link>
            <Link href="/operations">Operations</Link>
            <Link href="/providers">Providers</Link>
          </nav>
          <div className="mode"><i /> Provider control</div>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
