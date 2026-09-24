import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobForm } from "@/components/job-form";

const {
  push,
  createJob,
  fetchBitbucketRepositories,
  fetchGitHubRepositories,
  fetchGitLabRepositories,
  fetchRepos,
} = vi.hoisted(() => ({
  push: vi.fn(),
  createJob: vi.fn(),
  fetchBitbucketRepositories: vi.fn(),
  fetchGitHubRepositories: vi.fn(),
  fetchGitLabRepositories: vi.fn(),
  fetchRepos: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push,
  }),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    createJob,
    fetchBitbucketRepositories,
    fetchGitHubRepositories,
    fetchGitLabRepositories,
    fetchRepos,
  };
});

describe("JobForm", () => {
  beforeEach(() => {
    push.mockReset();
    createJob.mockReset();
    fetchGitHubRepositories.mockReset();
    fetchGitLabRepositories.mockReset();
    // Most tests here don't care about GitLab specifically — default to
    // "not connected" (mirroring the GitHub default below) so each existing
    // test doesn't have to configure it individually; tests that do care
    // override this with their own mockResolvedValue/mockRejectedValue.
    fetchGitLabRepositories.mockRejectedValue(
      new Error("Connect your GitLab account before loading repositories."),
    );
    fetchBitbucketRepositories.mockReset();
    fetchBitbucketRepositories.mockRejectedValue(
      new Error("Connect your Bitbucket account before loading repositories."),
    );
    fetchRepos.mockReset();
    sessionStorage.clear();
  });

  it("shows configured repos and submits a job", async () => {
    fetchRepos.mockResolvedValue({
      repos: [
        {
          name: "hello-world-sample",
          url: "https://github.com/octocat/Hello-World",
        },
      ],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: true,
        allowed_roots: ["D:\\repos"],
        allow_dirty: false,
        require_ticket_branch_match: true,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );
    createJob.mockResolvedValue({ job_id: "job-123" });

    render(<JobForm />);

    await screen.findByText("hello-world-sample");
    fireEvent.change(screen.getByLabelText(/Jira ticket key or URL/i), {
      target: { value: "PROJ-42" },
    });
    fireEvent.change(
      screen.getByLabelText(/Repository URL or pre-configured name/i),
      {
        target: { value: "hello-world-sample" },
      },
    );
    fireEvent.change(
      screen.getByLabelText(/Additional technical considerations/i),
      {
        target: { value: "Reuse the existing retry helper." },
      },
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: "PROJ-42",
        repo: "hello-world-sample",
        planning_notes: "Reuse the existing retry helper.",
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-123");
  });

  it("submits an optional base branch alongside the repo", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );
    createJob.mockResolvedValue({ job_id: "job-base-branch" });

    render(<JobForm />);

    await screen.findByText("github.com");
    fireEvent.change(screen.getByLabelText(/Jira ticket key or URL/i), {
      target: { value: "PROJ-42" },
    });
    fireEvent.change(
      screen.getByLabelText(/Repository URL or pre-configured name/i),
      {
        target: { value: "https://github.com/acme/repo" },
      },
    );
    fireEvent.change(screen.getByLabelText(/Base branch/i), {
      target: { value: "develop" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: "PROJ-42",
        repo: "https://github.com/acme/repo",
        base_branch: "develop",
        planning_notes: "",
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-base-branch");
  });

  it("renders backend errors safely", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );
    createJob.mockRejectedValue(
      new Error("The repo field looks like it contains a credential."),
    );

    render(<JobForm />);

    await screen.findByText("github.com");
    fireEvent.change(screen.getByLabelText(/Jira ticket key or URL/i), {
      target: { value: "PROJ-42" },
    });
    fireEvent.change(
      screen.getByLabelText(/Repository URL or pre-configured name/i),
      {
        target: { value: "bad-value" },
      },
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() => expect(createJob).toHaveBeenCalled());
    await screen.findByText(/repo field looks like it contains a credential/i);
  });

  it("submits an approved local repo path when local mode is enabled", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: true,
        allowed_roots: ["D:\\repos", "D:\\workspaces"],
        allow_dirty: false,
        require_ticket_branch_match: true,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );
    createJob.mockResolvedValue({ job_id: "job-local" });

    render(<JobForm />);

    await screen.findByText("D:\\repos");
    fireEvent.click(screen.getByRole("button", { name: /Local repo path/i }));
    fireEvent.change(screen.getByLabelText(/Jira ticket key or URL/i), {
      target: { value: "KAN-25" },
    });
    fireEvent.change(screen.getByLabelText(/Local repository path/i), {
      target: { value: "D:\\repos\\my-service" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: "KAN-25",
        repo: "D:\\repos\\my-service",
        planning_notes: "",
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-local");
  });

  it("mentions plain source folders when the server allows them", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: true,
        allowed_roots: ["D:\\repos"],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: true,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );

    render(<JobForm />);

    await screen.findByText("D:\\repos");
    fireEvent.click(screen.getByRole("button", { name: /Local repo path/i }));

    await screen.findByText(/plain source folder \(no \.git required\)/i);
    expect(
      screen.getByText(
        /Non-git folders:\s*allowed \(no branch\/dirty check\)/i,
      ),
    ).toBeInTheDocument();
  });

  it("shows connected GitHub repos and submits the selected clone url", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockResolvedValue({
      repos: [
        {
          id: 1001,
          name: "repo-one",
          full_name: "octocat/repo-one",
          html_url: "https://github.com/octocat/repo-one",
          clone_url: "https://github.com/octocat/repo-one.git",
          default_branch: "main",
          owner_login: "octocat",
          private: false,
        },
      ],
    });
    createJob.mockResolvedValue({ job_id: "job-github" });

    render(<JobForm />);

    await screen.findByText("Connected repositories");
    fireEvent.click(screen.getByRole("button", { name: "octocat/repo-one" }));
    fireEvent.change(screen.getByLabelText(/Jira ticket key or URL/i), {
      target: { value: "PROJ-77" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: "PROJ-77",
        repo: "https://github.com/octocat/repo-one.git",
        planning_notes: "",
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-github");
  });

  it("shows connected GitLab repos and submits the selected clone url", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["gitlab.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );
    fetchGitLabRepositories.mockResolvedValue({
      repos: [
        {
          id: 2001,
          name: "project-one",
          path_with_namespace: "octocat/project-one",
          web_url: "https://gitlab.com/octocat/project-one",
          http_url_to_repo: "https://gitlab.com/octocat/project-one.git",
          default_branch: "main",
          namespace: "octocat",
          private: false,
        },
      ],
    });
    createJob.mockResolvedValue({ job_id: "job-gitlab" });

    render(<JobForm />);

    await screen.findByText("Connected repositories");
    fireEvent.click(
      screen.getByRole("button", { name: "octocat/project-one" }),
    );
    fireEvent.change(screen.getByLabelText(/Jira ticket key or URL/i), {
      target: { value: "PROJ-77" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: "PROJ-77",
        repo: "https://gitlab.com/octocat/project-one.git",
        planning_notes: "",
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-gitlab");
  });

  it("treats a stale/rejected GitHub token as no quick-picks, not a page error, and still loads GitLab repos", async () => {
    // Regression test: a connected-but-rejected GitHub token (expired,
    // revoked, insufficient scope, ...) fails with a *different* message
    // than "never connected" — the form must not surface that as a
    // page-level error banner, and must not let it block the GitLab fetch
    // that runs right after it in the same effect.
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["gitlab.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error(
        "GitHub rejected repository access for this connection. Reconnect your GitHub account.",
      ),
    );
    fetchGitLabRepositories.mockResolvedValue({
      repos: [
        {
          id: 3001,
          name: "project-two",
          path_with_namespace: "octocat/project-two",
          web_url: "https://gitlab.com/octocat/project-two",
          http_url_to_repo: "https://gitlab.com/octocat/project-two.git",
          default_branch: "main",
          namespace: "octocat",
          private: false,
        },
      ],
    });

    render(<JobForm />);

    await screen.findByText("Connected repositories");
    expect(
      screen.queryByText(/GitHub rejected repository access/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /octocat\/project-two/i }),
    ).toBeInTheDocument();
  });

  it("shows connected repos in separate GitHub/GitLab tabs, one list visible at a time", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com", "gitlab.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockResolvedValue({
      repos: [
        {
          id: 1001,
          name: "repo-one",
          full_name: "octocat/repo-one",
          html_url: "https://github.com/octocat/repo-one",
          clone_url: "https://github.com/octocat/repo-one.git",
          default_branch: "main",
          owner_login: "octocat",
          private: false,
        },
      ],
    });
    fetchGitLabRepositories.mockResolvedValue({
      repos: [
        {
          id: 2001,
          name: "project-one",
          path_with_namespace: "octocat/project-one",
          web_url: "https://gitlab.com/octocat/project-one",
          http_url_to_repo: "https://gitlab.com/octocat/project-one.git",
          default_branch: "main",
          namespace: "octocat",
          private: false,
        },
      ],
    });

    render(<JobForm />);

    // GitHub tab is active by default; only its repos are visible.
    await screen.findByRole("button", { name: "octocat/repo-one" });
    expect(
      screen.queryByRole("button", { name: "octocat/project-one" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /GitLab/i }));

    // Switching tabs swaps the visible list, not accumulates both.
    await screen.findByRole("button", { name: "octocat/project-one" });
    expect(
      screen.queryByRole("button", { name: "octocat/repo-one" }),
    ).not.toBeInTheDocument();
  });

  it("shows a Bitbucket tab and submits the picked Bitbucket clone URL", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com", "bitbucket.org"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockResolvedValue({
      repos: [
        {
          id: 1001,
          name: "repo-one",
          full_name: "octocat/repo-one",
          html_url: "https://github.com/octocat/repo-one",
          clone_url: "https://github.com/octocat/repo-one.git",
          default_branch: "main",
          owner_login: "octocat",
          private: false,
        },
      ],
    });
    fetchBitbucketRepositories.mockResolvedValue({
      repos: [
        {
          uuid: "{repo-uuid}",
          name: "jfive",
          full_name: "jeena1/jfive",
          web_url: "https://bitbucket.org/jeena1/jfive",
          clone_url: "https://bitbucket.org/jeena1/jfive.git",
          default_branch: "main",
          workspace: "jeena1",
          private: true,
        },
      ],
    });
    createJob.mockResolvedValue({ job_id: "job-bitbucket" });

    render(<JobForm />);

    await screen.findByRole("button", { name: "octocat/repo-one" });
    // GitLab returned nothing, so it gets no (permanently empty) tab.
    expect(
      screen.queryByRole("tab", { name: /GitLab/i }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /Bitbucket/i }));
    fireEvent.click(
      await screen.findByRole("button", { name: "jeena1/jfive" }),
    );
    fireEvent.change(screen.getByLabelText(/Jira ticket key or URL/i), {
      target: { value: "PROJ-88" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: "PROJ-88",
        repo: "https://bitbucket.org/jeena1/jfive.git",
        planning_notes: "",
      }),
    );
  });

  it("prefills from a retry draft left by the job-detail page and clears it", async () => {
    sessionStorage.setItem(
      "jira2pullreq:retry-draft",
      JSON.stringify({
        ticket: "KAN-29",
        repo: "D:\\repos\\abtf-membership",
        repoMode: "local",
        planningNotes: "Use the shared logger, not print().",
        implementationClarifications: "",
      }),
    );
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: true,
        allowed_roots: ["D:\\repos"],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );

    render(<JobForm />);

    expect(await screen.findByDisplayValue("KAN-29")).toBeInTheDocument();
    expect(
      screen.getByDisplayValue("D:\\repos\\abtf-membership"),
    ).toBeInTheDocument();
    expect(
      screen.getByDisplayValue("Use the shared logger, not print()."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Local repo path/i }),
    ).toHaveAttribute("aria-selected", "true");
    expect(sessionStorage.getItem("jira2pullreq:retry-draft")).toBeNull();
  });

  it("forwards retried implementation clarifications to the new job's pending-clarifications store", async () => {
    sessionStorage.setItem(
      "jira2pullreq:retry-draft",
      JSON.stringify({
        ticket: "KAN-34",
        repo: "D:\\repos\\j5-frontend",
        repoMode: "local",
        planningNotes: "",
        implementationClarifications:
          "Stuff Type will be form based field i.e. F=factory, D=dock.",
      }),
    );
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: true,
        allowed_roots: ["D:\\repos"],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );
    createJob.mockResolvedValue({ job_id: "job-retried" });

    render(<JobForm />);

    await screen.findByDisplayValue("KAN-34");
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() => expect(createJob).toHaveBeenCalled());
    expect(push).toHaveBeenCalledWith("/jobs/job-retried");
    expect(
      sessionStorage.getItem("jira2pullreq:pending-clarifications:job-retried"),
    ).toBe("Stuff Type will be form based field i.e. F=factory, D=dock.");
  });

  it("switches to a file upload when Upload document is selected", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );

    render(<JobForm />);

    await screen.findByText("github.com");
    expect(
      screen.getByLabelText(/Jira ticket key or URL/i),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Upload document" }));

    expect(
      screen.queryByLabelText(/Jira ticket key or URL/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByLabelText(/Requirement document \(PDF\)/i),
    ).toBeInTheDocument();
  });

  it("submits a job with an uploaded requirement document instead of a ticket", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );
    createJob.mockResolvedValue({ job_id: "job-doc" });

    render(<JobForm />);

    await screen.findByText("github.com");
    fireEvent.click(screen.getByRole("button", { name: "Upload document" }));
    fireEvent.change(
      screen.getByLabelText(/Repository URL or pre-configured name/i),
      { target: { value: "hello-world-sample" } },
    );
    const file = new File(["%PDF-1.4 fake content"], "requirements.pdf", {
      type: "application/pdf",
    });
    const fileInput = screen.getByLabelText(/Requirement document \(PDF\)/i);
    await userEvent.upload(fileInput, file);
    // jsdom does not correctly compute constraint validity for a required
    // file input even once populated (a known jsdom limitation, not a real
    // browser behavior) — dispatch the submit event directly rather than
    // clicking the button, which would otherwise be blocked by jsdom's
    // (incorrect) native validation.
    fireEvent.submit(fileInput.closest("form")!);

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: undefined,
        repo: "hello-world-sample",
        planning_notes: "",
        requirement_document: file,
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-doc");
  });

  it("forwards a typed document ticket key alongside the uploaded document", async () => {
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );
    createJob.mockResolvedValue({ job_id: "job-doc-with-key" });

    render(<JobForm />);

    await screen.findByText("github.com");
    fireEvent.click(screen.getByRole("button", { name: "Upload document" }));
    fireEvent.change(
      screen.getByLabelText(/Repository URL or pre-configured name/i),
      { target: { value: "hello-world-sample" } },
    );
    fireEvent.change(screen.getByLabelText(/Jira ticket key, if known/i), {
      target: { value: "kan-31" },
    });
    const file = new File(["%PDF-1.4 fake content"], "requirements.pdf", {
      type: "application/pdf",
    });
    const fileInput = screen.getByLabelText(/Requirement document \(PDF\)/i);
    await userEvent.upload(fileInput, file);
    fireEvent.submit(fileInput.closest("form")!);

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: undefined,
        repo: "hello-world-sample",
        planning_notes: "",
        requirement_document: file,
        document_ticket_key: "kan-31",
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-doc-with-key");
  });

  it("prefills document mode from a retry draft for a document-sourced job", async () => {
    sessionStorage.setItem(
      "jira2pullreq:retry-draft",
      JSON.stringify({
        ticket: "",
        repo: "https://github.com/acme/repo.git",
        repoMode: "remote",
        planningNotes: "",
        requirementSource: "DOCUMENT",
        requirementDocumentName: "requirements.pdf",
        documentTicketKey: "KAN-31",
        implementationClarifications: "",
      }),
    );
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );

    render(<JobForm />);

    await screen.findByText("github.com");
    expect(
      screen.getByRole("button", { name: "Upload document" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByText(/Re-upload requirements\.pdf/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/Jira ticket key, if known/i)).toHaveValue(
      "KAN-31",
    );
  });

  it("prefills document mode from an older retry draft missing the ticket-key field", async () => {
    // A draft saved before this field existed (sessionStorage isn't
    // versioned) must not crash or render an uncontrolled-input warning.
    sessionStorage.setItem(
      "jira2pullreq:retry-draft",
      JSON.stringify({
        ticket: "",
        repo: "https://github.com/acme/repo.git",
        repoMode: "remote",
        planningNotes: "",
        requirementSource: "DOCUMENT",
        requirementDocumentName: "requirements.pdf",
        implementationClarifications: "",
      }),
    );
    fetchRepos.mockResolvedValue({
      repos: [],
      allowed_hosts: ["github.com"],
      local_repo_support: {
        enabled: false,
        allowed_roots: [],
        allow_dirty: false,
        require_ticket_branch_match: false,
        allow_non_git_folders: false,
      },
    });
    fetchGitHubRepositories.mockRejectedValue(
      new Error("Connect your GitHub account before loading repositories."),
    );

    render(<JobForm />);

    await screen.findByText("github.com");
    expect(screen.getByLabelText(/Jira ticket key, if known/i)).toHaveValue("");
  });
});
