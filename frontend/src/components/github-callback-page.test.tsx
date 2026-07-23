import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GitHubCallbackPage } from "@/components/github-callback-page";

const { completeGitHubConnect } = vi.hoisted(() => ({
  completeGitHubConnect: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    completeGitHubConnect,
  };
});

describe("GitHubCallbackPage", () => {
  beforeEach(() => {
    completeGitHubConnect.mockReset();
  });

  it("completes the GitHub callback and redirects to the app", async () => {
    const redirectTo = vi.fn();
    completeGitHubConnect.mockResolvedValue({
      ok: true,
      connection: {
        provider: "GITHUB",
        auth_kind: "OAUTH_USER",
        account_name: "octocat",
        account_id: "12345",
        account_url: "https://github.com/octocat",
        scopes: ["repo", "read:user"],
        installation_id: null,
        connected_at: "2026-07-21T12:00:00Z",
        updated_at: "2026-07-21T12:05:00Z",
        access_token_expires_at: null,
        has_refresh_token: false,
      },
    });

    render(
      <GitHubCallbackPage
        code="github-code"
        error={null}
        redirectDelayMs={0}
        redirectTo={redirectTo}
        state="state-123"
      />,
    );

    await screen.findByText(/Connected GitHub account octocat/i);
    await waitFor(() =>
      expect(completeGitHubConnect).toHaveBeenCalledWith(
        { code: "github-code", state: "state-123" },
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith(
        "/?github=connected&github_account=octocat",
      ),
    );
  });

  it("shows a friendly error when the callback params are missing", async () => {
    render(<GitHubCallbackPage code={null} error={null} state={null} />);

    await screen.findByText(/missing required parameters/i);
    expect(completeGitHubConnect).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: /Return to app/i })).toHaveAttribute(
      "href",
      "/?github=connect_failed",
    );
  });
});
