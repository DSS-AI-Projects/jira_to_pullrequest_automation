import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AuthGate } from "@/components/auth-gate";

import "./globals.css";

export const metadata: Metadata = {
  title: "jira2pullreq",
  description: "Jira ticket to implementation plan review",
};

export default function RootLayout(props: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AuthGate>{props.children}</AuthGate>
      </body>
    </html>
  );
}
