import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthGate } from "@/components/auth-gate";

const {
  devLogin,
  disconnectJira,
  disconnectRepoHostingProvider,
  fetchJiraAuthStatus,
  fetchRepoHostingStatus,
  fetchSession,
  logout,
  redirectBrowser,
  startBitbucketConnect,
  startGitHubConnect,
  startGitLabConnect,
  startJiraConnect,
} = vi.hoisted(() => ({
  devLogin: vi.fn(),
  disconnectJira: vi.fn(),
  disconnectRepoHostingProvider: vi.fn(),
  fetchJiraAuthStatus: vi.fn(),
  fetchRepoHostingStatus: vi.fn(),
  fetchSession: vi.fn(),
  logout: vi.fn(),
  redirectBrowser: vi.fn(),
  startBitbucketConnect: vi.fn(),
  startGitHubConnect: vi.fn(),
  startGitLabConnect: vi.fn(),
  startJiraConnect: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    devLogin,
    disconnectJira,
    disconnectRepoHostingProvider,
    fetchJiraAuthStatus,
    fetchRepoHostingStatus,
    fetchSession,
    logout,
    redirectBrowser,
    startBitbucketConnect,
    startGitHubConnect,
    startGitLabConnect,
    startJiraConnect,
  };
});

describe("AuthGate", () => {
  beforeEach(() => {
    devLogin.mockReset();
    disconnectJira.mockReset();
    disconnectRepoHostingProvider.mockReset();
    fetchJiraAuthStatus.mockReset();
    fetchRepoHostingStatus.mockReset();
    fetchSession.mockReset();
    logout.mockReset();
    redirectBrowser.mockReset();
    startBitbucketConnect.mockReset();
    startGitHubConnect.mockReset();
    startGitLabConnect.mockReset();
    startJiraConnect.mockReset();
    window.history.replaceState({}, "", "/");
  });

  it("renders children when auth is disabled", async () => {
    fetchSession.mockResolvedValue({
      auth_enabled: false,
      can_dev_login: false,
      user: null,
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText("App content");
  });

  it("shows sign-in form and completes dev login", async () => {
    fetchSession.mockResolvedValueOnce({
      auth_enabled: true,
      can_dev_login: true,
      user: null,
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [],
    });
    devLogin.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText(/Sign in to continue/i);
    fireEvent.change(screen.getByLabelText(/Email/i), {
      target: { value: "sam@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/Display name/i), {
      target: { value: "Sam" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Sign in/i }));

    await waitFor(() =>
      expect(devLogin).toHaveBeenCalledWith({
        email: "sam@example.com",
        display_name: "Sam",
      }),
    );
    await waitFor(() => expect(fetchJiraAuthStatus).toHaveBeenCalled());
    await screen.findByText("App content");
    await screen.findByText("sam@example.com");
    expect(screen.getByRole("link", { name: "New job" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(screen.getByRole("link", { name: "My jobs" })).toHaveAttribute(
      "href",
      "/jobs",
    );
  });

  it("shows Jira connect action for signed-in users without a personal connection", async () => {
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    startJiraConnect.mockResolvedValue({
      authorization_url: "https://auth.atlassian.com/authorize?state=abc",
    });
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [],
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText("Connect Jira");
    fireEvent.click(screen.getByRole("button", { name: "Connect Jira" }));

    await waitFor(() => expect(startJiraConnect).toHaveBeenCalled());
    expect(redirectBrowser).toHaveBeenCalledWith(
      "https://auth.atlassian.com/authorize?state=abc",
    );
  });

  it("shows Jira disconnect action for signed-in users with a personal connection", async () => {
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValueOnce({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "DELEGATED",
      connected: true,
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
    disconnectJira.mockResolvedValue(undefined);
    fetchJiraAuthStatus.mockResolvedValueOnce({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [],
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText("Connected to Acme Jira");
    fireEvent.click(screen.getByRole("button", { name: "Disconnect Jira" }));

    await waitFor(() => expect(disconnectJira).toHaveBeenCalled());
    await screen.findByText("Personal Jira not connected");
  });

  it("shows a Jira success banner after returning from the callback", async () => {
    window.history.replaceState(
      {},
      "",
      "/?jira=connected&jira_site=Acme%20Jira",
    );
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "DELEGATED",
      connected: true,
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
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [],
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText("Connected Jira site Acme Jira.");
    expect(window.location.search).toBe("");
  });

  it("shows a Jira failure banner after an unsuccessful callback return", async () => {
    window.history.replaceState({}, "", "/?jira=connect_failed");
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [],
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText(/Jira sign-in did not complete/i);
    expect(window.location.search).toBe("");
  });

  it("shows repository provider status for configured and disabled providers", async () => {
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [
        {
          provider: "GITHUB",
          display_name: "GitHub",
          enabled: true,
          configured: true,
          connected: false,
          connection: null,
        },
        {
          provider: "GITLAB",
          display_name: "GitLab",
          enabled: false,
          configured: false,
          connected: false,
          connection: null,
        },
      ],
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText("Repository access");
    await screen.findByRole("button", { name: "Connect GitHub" });
    await screen.findByText(/GitLab connections are not enabled/i);
  });

  it("starts the GitHub connect flow for configured GitHub access", async () => {
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [
        {
          provider: "GITHUB",
          display_name: "GitHub",
          enabled: true,
          configured: true,
          connected: false,
          connection: null,
        },
      ],
    });
    startGitHubConnect.mockResolvedValue({
      authorization_url:
        "https://github.com/login/oauth/authorize?state=github123",
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitHub" }),
    );

    await waitFor(() => expect(startGitHubConnect).toHaveBeenCalled());
    expect(redirectBrowser).toHaveBeenCalledWith(
      "https://github.com/login/oauth/authorize?state=github123",
    );
  });

  it("starts the GitLab connect flow for configured GitLab access", async () => {
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [
        {
          provider: "GITLAB",
          display_name: "GitLab",
          enabled: true,
          configured: true,
          connected: false,
          connection: null,
        },
      ],
    });
    startGitLabConnect.mockResolvedValue({
      authorization_url: "https://gitlab.com/oauth/authorize?state=gitlab123",
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitLab" }),
    );

    await waitFor(() => expect(startGitLabConnect).toHaveBeenCalled());
    expect(redirectBrowser).toHaveBeenCalledWith(
      "https://gitlab.com/oauth/authorize?state=gitlab123",
    );
  });

  it("starts the Bitbucket connect flow for configured Bitbucket access", async () => {
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValue({
      providers: [
        {
          provider: "BITBUCKET",
          display_name: "Bitbucket",
          enabled: true,
          configured: true,
          connected: false,
          connection: null,
        },
      ],
    });
    startBitbucketConnect.mockResolvedValue({
      authorization_url:
        "https://bitbucket.org/site/oauth2/authorize?state=bitbucket123",
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Bitbucket" }),
    );

    await waitFor(() => expect(startBitbucketConnect).toHaveBeenCalled());
    expect(redirectBrowser).toHaveBeenCalledWith(
      "https://bitbucket.org/site/oauth2/authorize?state=bitbucket123",
    );
  });

  it("shows a success flash after returning from the Bitbucket callback", async () => {
    window.history.replaceState(
      {},
      "",
      "/?bitbucket=connected&bitbucket_account=jeena1",
    );
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: false,
      oauth_configured: false,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValue({ providers: [] });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText("Connected Bitbucket account jeena1.");
    expect(window.location.search).toBe("");
  });

  it("disconnects a connected repository provider", async () => {
    fetchSession.mockResolvedValue({
      auth_enabled: true,
      can_dev_login: true,
      user: {
        id: "user-1",
        email: "sam@example.com",
        display_name: "Sam",
        role: "USER",
      },
    });
    fetchJiraAuthStatus.mockResolvedValue({
      oauth_enabled: true,
      oauth_configured: true,
      shared_configured: true,
      effective_mode: "SHARED",
      connected: false,
      connection: null,
    });
    fetchRepoHostingStatus.mockResolvedValueOnce({
      providers: [
        {
          provider: "GITHUB",
          display_name: "GitHub",
          enabled: true,
          configured: true,
          connected: true,
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
        },
      ],
    });
    disconnectRepoHostingProvider.mockResolvedValue(undefined);
    fetchRepoHostingStatus.mockResolvedValueOnce({
      providers: [
        {
          provider: "GITHUB",
          display_name: "GitHub",
          enabled: true,
          configured: true,
          connected: false,
          connection: null,
        },
      ],
    });

    render(
      <AuthGate>
        <div>App content</div>
      </AuthGate>,
    );

    await screen.findByText("Connected as");
    fireEvent.click(screen.getByRole("button", { name: "Disconnect GitHub" }));

    await waitFor(() =>
      expect(disconnectRepoHostingProvider).toHaveBeenCalledWith("GITHUB"),
    );
    await screen.findByRole("button", { name: "Connect GitHub" });
  });
});
