import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "超級井字棋｜6,000局觀察研究室",
  description: "重播已驗證的6,000局雙AI棋譜，探索合法分支與同構，匿名提出研究建議。",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-Hant">
      <body className="antialiased">{children}</body>
    </html>
  );
}
