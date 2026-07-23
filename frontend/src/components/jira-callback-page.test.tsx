import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JiraCallbackPage } from "@/components/jira-callback-page";

const { completeJiraConnect } = vi.hoisted(() => ({
  completeJiraConnect: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    completeJiraConnect,
  };
});

describe("JiraCallbackPage", () => {
  beforeEach(() => {
    completeJiraConnect.mockReset();
  });

  it("completes the Jira callback and redirects to the app", async () => {
    const redirectTo = vi.fn();
    completeJiraConnect.mockResolvedValue({
      ok: true,
      connection: {
        site: {
          id: "cloud-123",
          name: "Acme Jira",
          url: "https://acme.atlassian.net",
        },
        scopes: ["read:jira-work", "offline_access"],
        connected_at: "2026-07-21T12:00:00Z",
        updated_at: "2026-07-21T12:05:00Z",
        access_token_expires_at: "2026-07-21T13:00:00Z",
        has_refresh_token: true,
      },
    });

    render(
      <JiraCallbackPage
        code="jira-code"
        error={null}
        redirectDelayMs={0}
        redirectTo={redirectTo}
        state="state-123"
      />,
    );

    await screen.findByText(/Connected Jira site Acme Jira/i);
    await waitFor(() =>
      expect(completeJiraConnect).toHaveBeenCalledWith(
        { code: "jira-code", state: "state-123" },
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith("/?jira=connected&jira_site=Acme+Jira"),
    );
  });

  it("shows a friendly error when the callback params are missing", async () => {
    render(
      <JiraCallbackPage code={null} error={null} state={null} />,
    );

    await screen.findByText(/missing required parameters/i);
    expect(completeJiraConnect).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: /Return to app/i })).toHaveAttribute(
      "href",
      "/?jira=connect_failed",
    );
  });
});
