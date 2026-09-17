import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GitLabCallbackPage } from "@/components/gitlab-callback-page";

const { completeGitLabConnect } = vi.hoisted(() => ({
  completeGitLabConnect: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    completeGitLabConnect,
  };
});

describe("GitLabCallbackPage", () => {
  beforeEach(() => {
    completeGitLabConnect.mockReset();
  });

  it("completes the GitLab callback and redirects to the app", async () => {
    const redirectTo = vi.fn();
    completeGitLabConnect.mockResolvedValue({
      ok: true,
      connection: {
        provider: "GITLAB",
        auth_kind: "OAUTH_USER",
        account_name: "octocat",
        account_id: "54321",
        account_url: "https://gitlab.com/octocat",
        scopes: ["read_api", "read_user"],
        installation_id: null,
        connected_at: "2026-07-21T12:00:00Z",
        updated_at: "2026-07-21T12:05:00Z",
        access_token_expires_at: "2026-07-21T14:00:00Z",
        has_refresh_token: true,
      },
    });

    render(
      <GitLabCallbackPage
        code="gitlab-code"
        error={null}
        redirectDelayMs={0}
        redirectTo={redirectTo}
        state="state-123"
      />,
    );

    await screen.findByText(/Connected GitLab account octocat/i);
    await waitFor(() =>
      expect(completeGitLabConnect).toHaveBeenCalledWith(
        { code: "gitlab-code", state: "state-123" },
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith(
        "/?gitlab=connected&gitlab_account=octocat",
      ),
    );
  });

  it("shows a friendly error when the callback params are missing", async () => {
    render(<GitLabCallbackPage code={null} error={null} state={null} />);

    await screen.findByText(/missing required parameters/i);
    expect(completeGitLabConnect).not.toHaveBeenCalled();
    expect(
      screen.getByRole("link", { name: /Return to app/i }),
    ).toHaveAttribute("href", "/?gitlab=connect_failed");
  });
});
