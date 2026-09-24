import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BitbucketCallbackPage } from "@/components/bitbucket-callback-page";

const { completeBitbucketConnect } = vi.hoisted(() => ({
  completeBitbucketConnect: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    completeBitbucketConnect,
  };
});

describe("BitbucketCallbackPage", () => {
  beforeEach(() => {
    completeBitbucketConnect.mockReset();
  });

  it("completes the Bitbucket callback and redirects to the app", async () => {
    const redirectTo = vi.fn();
    completeBitbucketConnect.mockResolvedValue({
      ok: true,
      connection: {
        provider: "BITBUCKET",
        auth_kind: "OAUTH_USER",
        account_name: "jeena1",
        account_id: "{user-uuid}",
        account_url: "https://bitbucket.org/jeena1/",
        scopes: ["account", "repository"],
        installation_id: null,
        connected_at: "2026-09-24T12:00:00Z",
        updated_at: "2026-09-24T12:05:00Z",
        access_token_expires_at: "2026-09-24T14:00:00Z",
        has_refresh_token: true,
      },
    });

    render(
      <BitbucketCallbackPage
        code="bitbucket-code"
        error={null}
        redirectDelayMs={0}
        redirectTo={redirectTo}
        state="state-123"
      />,
    );

    await screen.findByText(/Connected Bitbucket account jeena1/i);
    await waitFor(() =>
      expect(completeBitbucketConnect).toHaveBeenCalledWith({
        code: "bitbucket-code",
        state: "state-123",
      }),
    );
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith(
        "/?bitbucket=connected&bitbucket_account=jeena1",
      ),
    );
  });

  it("exchanges the code once under StrictMode and shows the real success", async () => {
    // Regression test: StrictMode's double-run effect used to send a second
    // exchange request with the same single-use code/state. The first one
    // connected the account, but the page showed the second one's "missing
    // or expired" failure — and "Return to app" then flashed "did not complete".
    const redirectTo = vi.fn();
    completeBitbucketConnect.mockImplementation(() => {
      if (completeBitbucketConnect.mock.calls.length > 1) {
        return Promise.reject(
          new Error(
            "That repository provider sign-in attempt is missing or expired.",
          ),
        );
      }
      return Promise.resolve({
        ok: true,
        connection: {
          provider: "BITBUCKET",
          auth_kind: "OAUTH_USER",
          account_name: "sambai",
          account_id: "{user-uuid}",
          account_url: "https://bitbucket.org/sambai/",
          scopes: ["account", "repository"],
          installation_id: null,
          connected_at: "2026-09-24T12:00:00Z",
          updated_at: "2026-09-24T12:05:00Z",
          access_token_expires_at: "2026-09-24T14:00:00Z",
          has_refresh_token: true,
        },
      });
    });

    render(
      <StrictMode>
        <BitbucketCallbackPage
          code="bitbucket-code"
          error={null}
          redirectDelayMs={0}
          redirectTo={redirectTo}
          state="strict-mode-state"
        />
      </StrictMode>,
    );

    await screen.findByText(/Connected Bitbucket account sambai/i);
    expect(completeBitbucketConnect).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/missing or expired/i)).not.toBeInTheDocument();
    await waitFor(() =>
      expect(redirectTo).toHaveBeenCalledWith(
        "/?bitbucket=connected&bitbucket_account=sambai",
      ),
    );
  });

  it("shows a friendly error when the callback params are missing", async () => {
    render(<BitbucketCallbackPage code={null} error={null} state={null} />);

    await screen.findByText(/missing required parameters/i);
    expect(completeBitbucketConnect).not.toHaveBeenCalled();
    expect(
      screen.getByRole("link", { name: /Return to app/i }),
    ).toHaveAttribute("href", "/?bitbucket=connect_failed");
  });
});
