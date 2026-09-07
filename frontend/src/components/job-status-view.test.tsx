import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobStatusView } from "@/components/job-status-view";

const { correctValidation, createBranch, fetchJob, implementJob, push } =
  vi.hoisted(() => ({
    correctValidation: vi.fn(),
    createBranch: vi.fn(),
    fetchJob: vi.fn(),
    implementJob: vi.fn(),
    push: vi.fn(),
  }));

vi.mock("next/link", () => ({
  default: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props} />
  ),
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
    correctValidation,
    createBranch,
    fetchJob,
    implementJob,
  };
});

describe("JobStatusView", () => {
  beforeEach(() => {
    correctValidation.mockReset();
    createBranch.mockReset();
    fetchJob.mockReset();
    implementJob.mockReset();
    push.mockReset();
  });

  it("shows approve button for local plan-ready jobs and renders implementation results", async () => {
    fetchJob
      .mockResolvedValueOnce({
        id: "job-123",
        ticket_key: "KAN-25",
        repo_url: "D:\\workspaces\\hello-world-debug",
        planning_notes: "Reuse the existing retry helper.",
        state: "PLAN_READY",
        error: null,
        repo_info: {
          source_kind: "LOCAL",
          branch: "KAN-25",
          commit_sha: "a".repeat(40),
          origin_url: "https://github.com/octocat/Hello-World.git",
          is_dirty: false,
          local_path: "D:\\workspaces\\hello-world-debug",
        },
        workspace_path: "D:\\workdir\\job-123\\repo",
        plan: {
          schema_version: 2,
          summary: "Update the greeting text.",
          ticket_type: "chore",
          estimated_story_points: 2,
          complexity_level: "low",
          impacted_files: [{ path: "README", reason: "Contains the greeting" }],
          proposed_changes: [
            {
              file: "README",
              action: "modify",
              description: "Replace Hello World text.",
            },
          ],
          test_strategy: "Inspect README manually.",
          risks: [],
          open_questions: [],
        },
        usage: {
          input_tokens: 100,
          output_tokens: 200,
          total_cost_usd: 0.1,
          num_turns: 3,
          duration_seconds: 4.2,
        },
        implementation_usage: null,
        implementation_result: null,
        implementation_diff: null,
        validation_results: [],
        implementation_clarifications: null,
        implementation_approved_at: null,
        implementation_started_at: null,
        implementation_finished_at: null,
        created_at: "2026-07-17T00:00:00Z",
        updated_at: "2026-07-17T00:00:00Z",
      })
      .mockResolvedValueOnce({
        id: "job-123",
        ticket_key: "KAN-25",
        repo_url: "D:\\workspaces\\hello-world-debug",
        planning_notes: "Reuse the existing retry helper.",
        state: "IMPLEMENTATION_READY",
        error: null,
        repo_info: {
          source_kind: "LOCAL",
          branch: "KAN-25",
          commit_sha: "a".repeat(40),
          origin_url: "https://github.com/octocat/Hello-World.git",
          is_dirty: false,
          local_path: "D:\\workspaces\\hello-world-debug",
        },
        workspace_path: "D:\\workdir\\job-123\\repo",
        plan: {
          schema_version: 2,
          summary: "Update the greeting text.",
          ticket_type: "chore",
          estimated_story_points: 2,
          complexity_level: "low",
          impacted_files: [{ path: "README", reason: "Contains the greeting" }],
          proposed_changes: [
            {
              file: "README",
              action: "modify",
              description: "Replace Hello World text.",
            },
          ],
          test_strategy: "Inspect README manually.",
          risks: [],
          open_questions: [],
        },
        usage: {
          input_tokens: 100,
          output_tokens: 200,
          total_cost_usd: 0.1,
          num_turns: 3,
          duration_seconds: 4.2,
        },
        implementation_usage: {
          input_tokens: 90,
          output_tokens: 60,
          total_cost_usd: 0.05,
          num_turns: 2,
          duration_seconds: 2.6,
        },
        implementation_result: {
          summary: "Updated README greeting.",
          changed_files: [
            {
              path: "README",
              action: "modify",
              rationale: "Updated the greeting text.",
            },
          ],
          warnings: [],
          follow_up_questions: ["Confirm punctuation."],
        },
        implementation_diff: {
          overall_patch:
            "diff --git a/README b/README\nindex 1111111..2222222 100644\n--- a/README\n+++ b/README\n@@ -1 +1 @@\n-Hello AI Agentic World\n+Hello Back To World\n",
          files: [
            {
              path: "README",
              patch:
                "diff --git a/README b/README\nindex 1111111..2222222 100644\n--- a/README\n+++ b/README\n@@ -1 +1 @@\n-Hello AI Agentic World\n+Hello Back To World\n",
              additions: 1,
              deletions: 1,
              is_binary: false,
            },
          ],
        },
        validation_results: [
          {
            name: "validation-profile",
            command: "",
            status: "SKIPPED",
            summary:
              "No recognized validation profile was detected for this repository.",
            output_excerpt: null,
          },
        ],
        implementation_clarifications: "Use British spelling in the greeting.",
        implementation_approved_at: "2026-07-17T00:02:00Z",
        implementation_started_at: "2026-07-17T00:02:01Z",
        implementation_finished_at: "2026-07-17T00:02:10Z",
        created_at: "2026-07-17T00:00:00Z",
        updated_at: "2026-07-17T00:02:10Z",
      });
    implementJob.mockResolvedValue({ job_id: "job-123" });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const createObjectURL = vi.fn().mockReturnValue("blob:mock-url");
    const revokeObjectURL = vi.fn();
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;

    render(<JobStatusView jobId="job-123" />);

    const button = await screen.findByRole("button", {
      name: /Approve and Implement/i,
    });
    const textarea = screen.getByPlaceholderText(
      /Use British spelling for user-facing copy/i,
    );
    fireEvent.change(textarea, {
      target: { value: "  Use British spelling in the greeting.  " },
    });
    fireEvent.click(button);

    await waitFor(() =>
      expect(implementJob).toHaveBeenCalledWith("job-123", {
        clarifications: "  Use British spelling in the greeting.  ",
      }),
    );
    await screen.findByText(/Updated README greeting/i);
    expect(screen.getByText(/validation results/i)).toBeInTheDocument();
    expect(screen.getByText(/Show full patch/i)).toBeInTheDocument();
    expect(screen.getByText(/\+1 -1/)).toBeInTheDocument();
    expect(screen.getByText(/Confirm punctuation/i)).toBeInTheDocument();
    expect(screen.getByText(/Implementation workspace/i)).toBeInTheDocument();
    expect(screen.getByText(/D:\\workdir\\job-123\\repo/i)).toBeInTheDocument();
    expect(screen.getByText(/\$0.15/)).toBeInTheDocument();
    expect(
      screen.getByText(/Guidance considered during implementation/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Use British spelling in the greeting\./i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Technical considerations you provided/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Reuse the existing retry helper\./i),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Copy patch/i }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        expect.stringContaining("Hello Back To World"),
      ),
    );
    await screen.findByRole("button", { name: /Copied!/i });

    fireEvent.click(screen.getByRole("button", { name: /Download patch/i }));
    expect(createObjectURL).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
  });

  it("allows implementation approval for remote jobs too", async () => {
    fetchJob.mockResolvedValue({
      id: "job-remote",
      ticket_key: "PROJ-1",
      repo_url: "git@github.com:acme/repo.git",
      planning_notes: null,
      state: "PLAN_READY",
      error: null,
      repo_info: {
        source_kind: "REMOTE",
        branch: "main",
        commit_sha: "b".repeat(40),
        origin_url: "https://github.com/acme/repo.git",
        is_dirty: false,
        local_path: null,
      },
      workspace_path: "D:\\workdir\\job-remote\\repo",
      plan: {
        schema_version: 2,
        summary: "Do the thing.",
        ticket_type: "feature",
        estimated_story_points: 3,
        complexity_level: "medium",
        impacted_files: [{ path: "a.py", reason: "Entry point" }],
        proposed_changes: [
          { file: "a.py", action: "modify", description: "Apply the change." },
        ],
        test_strategy: "Run unit tests.",
        risks: [],
        open_questions: [],
      },
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
    });

    render(<JobStatusView jobId="job-remote" />);

    await screen.findByRole("button", { name: /Approve and Implement/i });
  });

  it("allows implementation and renders folder-source metadata for a LOCAL_FOLDER job", async () => {
    fetchJob.mockResolvedValue({
      id: "job-folder",
      ticket_key: "PROJ-9",
      repo_url: "D:\\repos\\plain-folder",
      planning_notes: null,
      state: "PLAN_READY",
      error: null,
      repo_info: {
        source_kind: "LOCAL_FOLDER",
        branch: null,
        commit_sha: "c".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\plain-folder",
      },
      workspace_path: "D:\\workdir\\job-folder\\repo",
      plan: {
        schema_version: 2,
        summary: "Do the thing.",
        ticket_type: "feature",
        estimated_story_points: 3,
        complexity_level: "medium",
        impacted_files: [{ path: "a.py", reason: "Entry point" }],
        proposed_changes: [
          { file: "a.py", action: "modify", description: "Apply the change." },
        ],
        test_strategy: "Run unit tests.",
        risks: [],
        open_questions: [],
      },
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
    });

    render(<JobStatusView jobId="job-folder" />);

    await screen.findByRole("button", { name: /Approve and Implement/i });
    expect(screen.getByText("Local folder (no git)")).toBeInTheDocument();
    expect(screen.getByText("Not applicable")).toBeInTheDocument();
  });

  it("renders legacy implementation-ready jobs that omit validation results", async () => {
    fetchJob.mockResolvedValue({
      id: "job-legacy",
      ticket_key: "KAN-25",
      repo_url: "D:\\work\\2026\\AI\\Trae\\Hello-World",
      planning_notes: null,
      state: "IMPLEMENTATION_READY",
      error: null,
      repo_info: {
        source_kind: "LOCAL",
        branch: "KAN-25",
        commit_sha: "c".repeat(40),
        origin_url: "https://github.com/octocat/Hello-World.git",
        is_dirty: false,
        local_path: "D:\\work\\2026\\AI\\Trae\\Hello-World",
      },
      workspace_path: "D:\\workdir\\job-legacy\\repo",
      plan: {
        schema_version: 2,
        summary: "Update the greeting text.",
        ticket_type: "chore",
        estimated_story_points: 2,
        complexity_level: "low",
        impacted_files: [{ path: "README", reason: "Contains the greeting" }],
        proposed_changes: [
          {
            file: "README",
            action: "modify",
            description: "Replace Hello World text.",
          },
        ],
        test_strategy: "Inspect README manually.",
        risks: [],
        open_questions: [],
      },
      usage: null,
      implementation_usage: null,
      implementation_result: {
        summary: "Updated README greeting.",
        changed_files: [
          {
            path: "README",
            action: "modify",
            rationale: "Updated the greeting text.",
          },
        ],
        warnings: ["Check the filename mismatch."],
        follow_up_questions: [],
      },
      implementation_diff: undefined,
      validation_results: undefined,
      implementation_clarifications: null,
      implementation_approved_at: "2026-07-17T00:02:00Z",
      implementation_started_at: "2026-07-17T00:02:01Z",
      implementation_finished_at: "2026-07-17T00:02:10Z",
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:02:10Z",
    });

    render(<JobStatusView jobId="job-legacy" />);

    await screen.findByText(/Updated README greeting/i);
    expect(screen.getByText(/Validation results/i)).toBeInTheDocument();
    expect(
      screen.getByText(/No diff artifacts were recorded/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Check the filename mismatch/i),
    ).toBeInTheDocument();
  });

  it("saves a retry draft and navigates home when Retry is clicked on a failed job", async () => {
    sessionStorage.clear();
    fetchJob.mockResolvedValue({
      id: "job-failed",
      ticket_key: "KAN-29",
      repo_url: "D:\\repos\\abtf-membership",
      planning_notes: "Use the shared logger, not print().",
      state: "FAILED",
      error: {
        code: "BUDGET_EXCEEDED",
        message:
          "The planning agent exceeded its run budget before finishing a plan.",
        stage: "PLANNING",
      },
      repo_info: {
        source_kind: "LOCAL",
        branch: "KAN-29",
        commit_sha: "c".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\abtf-membership",
      },
      workspace_path: "D:\\workdir\\job-failed\\repo",
      plan: null,
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-08-05T17:54:02Z",
      updated_at: "2026-08-05T17:57:53Z",
    });

    render(<JobStatusView jobId="job-failed" />);

    const retryButton = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(retryButton);

    expect(
      JSON.parse(sessionStorage.getItem("jira2pullreq:retry-draft")!),
    ).toEqual({
      ticket: "KAN-29",
      repo: "D:\\repos\\abtf-membership",
      repoMode: "local",
      planningNotes: "Use the shared logger, not print().",
      implementationClarifications: "",
    });
    expect(push).toHaveBeenCalledWith("/");
  });

  it("carries implementation clarifications into the retry draft for a failed implementation job", async () => {
    sessionStorage.clear();
    fetchJob.mockResolvedValue({
      id: "job-impl-failed-clarified",
      ticket_key: "KAN-34",
      repo_url: "D:\\repos\\j5-frontend",
      planning_notes: null,
      state: "IMPLEMENTATION_FAILED",
      error: {
        code: "VALIDATION_FAILED",
        message: "Running post-implementation validation failed unexpectedly.",
        stage: "VALIDATING",
      },
      repo_info: {
        source_kind: "LOCAL",
        branch: "KAN-34",
        commit_sha: "a".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\j5-frontend",
      },
      workspace_path: "D:\\workdir\\job-impl-failed-clarified\\repo",
      plan: null,
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications:
        "Stuff Type will be form based field i.e. F=factory, D=dock.",
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-08-31T11:33:26Z",
      updated_at: "2026-08-31T12:18:42Z",
    });

    render(<JobStatusView jobId="job-impl-failed-clarified" />);

    const retryButton = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(retryButton);

    expect(
      JSON.parse(sessionStorage.getItem("jira2pullreq:retry-draft")!),
    ).toEqual({
      ticket: "KAN-34",
      repo: "D:\\repos\\j5-frontend",
      repoMode: "local",
      planningNotes: "",
      implementationClarifications:
        "Stuff Type will be form based field i.e. F=factory, D=dock.",
    });
    expect(push).toHaveBeenCalledWith("/");
  });

  it("prefills clarifications left by a retried job's creation flow and consumes them once", async () => {
    sessionStorage.setItem(
      "jira2pullreq:pending-clarifications:job-123",
      "Stuff Type will be form based field i.e. F=factory, D=dock.",
    );
    fetchJob.mockResolvedValue({
      id: "job-123",
      ticket_key: "KAN-34",
      repo_url: "D:\\repos\\j5-frontend",
      planning_notes: null,
      state: "PLAN_READY",
      error: null,
      repo_info: {
        source_kind: "LOCAL",
        branch: "KAN-34",
        commit_sha: "a".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\j5-frontend",
      },
      workspace_path: "D:\\workdir\\job-123\\repo",
      plan: {
        schema_version: 2,
        summary: "Add the Stuff Type field.",
        ticket_type: "feature",
        estimated_story_points: 3,
        complexity_level: "medium",
        impacted_files: [],
        proposed_changes: [],
        test_strategy: "n/a",
        risks: [],
        open_questions: [],
      },
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-08-31T11:33:26Z",
      updated_at: "2026-08-31T11:33:26Z",
    });

    render(<JobStatusView jobId="job-123" />);

    const textarea = await screen.findByPlaceholderText(
      /Use British spelling for user-facing copy/i,
    );
    expect(textarea).toHaveValue(
      "Stuff Type will be form based field i.e. F=factory, D=dock.",
    );
    expect(
      sessionStorage.getItem("jira2pullreq:pending-clarifications:job-123"),
    ).toBeNull();
  });

  it("highlights the failed step in red for an IMPLEMENTATION_FAILED job", async () => {
    fetchJob.mockResolvedValue({
      id: "job-impl-failed",
      ticket_key: "KAN-31",
      repo_url: "D:\\repos\\abtf-membership",
      planning_notes: null,
      state: "IMPLEMENTATION_FAILED",
      error: {
        code: "IMPLEMENTATION_INVALID",
        message:
          "The implementation agent could not produce a valid result for this plan. " +
          "Try approving implementation again, or add clarifications to help it succeed.",
        stage: "IMPLEMENTING",
      },
      repo_info: {
        source_kind: "LOCAL",
        branch: "jira_to_code_test",
        commit_sha: "f".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\abtf-membership",
      },
      workspace_path: "D:\\workdir\\job-impl-failed\\repo",
      plan: null,
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-08-20T09:00:00Z",
      updated_at: "2026-08-20T09:05:00Z",
    });

    render(<JobStatusView jobId="job-impl-failed" />);

    const failedLabel = await screen.findByText("Failed here");
    expect(failedLabel).toHaveClass("timeline-failed-label");
    expect(failedLabel.closest("li")).toHaveClass("is-failed");

    const stateValue = await screen.findByText("Implementation failed");
    expect(stateValue).toHaveClass("state-failed");
  });

  it("shows partial changes captured before an implementation failure", async () => {
    fetchJob.mockResolvedValue({
      id: "job-impl-failed-partial",
      ticket_key: "KAN-31",
      repo_url: "D:\\repos\\abtf-membership",
      planning_notes: null,
      state: "IMPLEMENTATION_FAILED",
      error: {
        code: "BUDGET_EXCEEDED",
        message:
          "The implementation agent exceeded its run budget before finishing.",
        stage: "IMPLEMENTING",
      },
      repo_info: {
        source_kind: "LOCAL",
        branch: "jira_to_code_test",
        commit_sha: "f".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\abtf-membership",
      },
      workspace_path: "D:\\workdir\\job-impl-failed-partial\\repo",
      plan: null,
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: {
        overall_patch:
          "diff --git a/README b/README\nindex 1111111..2222222 100644\n--- a/README\n+++ b/README\n@@ -1 +1 @@\n-Hello World\n+Hello Partial World\n",
        files: [
          {
            path: "README",
            patch:
              "diff --git a/README b/README\nindex 1111111..2222222 100644\n--- a/README\n+++ b/README\n@@ -1 +1 @@\n-Hello World\n+Hello Partial World\n",
            additions: 1,
            deletions: 1,
            is_binary: false,
          },
        ],
      },
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-08-20T09:00:00Z",
      updated_at: "2026-08-20T09:05:00Z",
    });

    render(<JobStatusView jobId="job-impl-failed-partial" />);

    await screen.findByText("Changes made before the failure");
    expect(screen.getByText("README")).toBeInTheDocument();
    expect(screen.getByText(/\+1 -1/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Copy patch" }),
    ).toBeInTheDocument();
  });

  it("does not show a partial-changes section when nothing was captured", async () => {
    fetchJob.mockResolvedValue({
      id: "job-impl-failed-empty",
      ticket_key: "KAN-31",
      repo_url: "D:\\repos\\abtf-membership",
      planning_notes: null,
      state: "IMPLEMENTATION_FAILED",
      error: {
        code: "IMPLEMENTATION_INVALID",
        message: "The implementation agent could not produce a valid result.",
        stage: "IMPLEMENTING",
      },
      repo_info: {
        source_kind: "LOCAL",
        branch: "jira_to_code_test",
        commit_sha: "f".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\abtf-membership",
      },
      workspace_path: "D:\\workdir\\job-impl-failed-empty\\repo",
      plan: null,
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-08-20T09:00:00Z",
      updated_at: "2026-08-20T09:05:00Z",
    });

    render(<JobStatusView jobId="job-impl-failed-empty" />);

    await screen.findByText("Job failed");
    expect(
      screen.queryByText("Changes made before the failure"),
    ).not.toBeInTheDocument();
  });

  function readyJobWithValidation(overrides: Record<string, unknown>) {
    return {
      id: "job-correction",
      ticket_key: "KAN-31",
      repo_url: "D:\\repos\\abtf-membership",
      planning_notes: null,
      state: "IMPLEMENTATION_READY",
      error: null,
      repo_info: {
        source_kind: "LOCAL",
        branch: "jira_to_code_test",
        commit_sha: "f".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\abtf-membership",
      },
      workspace_path: "D:\\workdir\\job-correction\\repo",
      plan: {
        schema_version: 2,
        summary: "Add a Program Type filter.",
        ticket_type: "feature",
        estimated_story_points: 3,
        complexity_level: "medium",
        impacted_files: [],
        proposed_changes: [],
        test_strategy: "n/a",
        risks: [],
        open_questions: [],
      },
      usage: null,
      implementation_usage: null,
      implementation_result: {
        summary: "Added the Program Type filter.",
        changed_files: [],
        warnings: [],
        follow_up_questions: [],
      },
      implementation_diff: { overall_patch: "", files: [] },
      validation_results: [
        {
          name: "ruff",
          command: "ruff check .",
          status: "FAILED",
          summary: "1 lint error",
          output_excerpt: "fix.py:1: undefined name",
        },
      ],
      implementation_clarifications: null,
      implementation_approved_at: "2026-08-20T09:02:00Z",
      implementation_started_at: "2026-08-20T09:02:01Z",
      implementation_finished_at: "2026-08-20T09:02:10Z",
      implementation_baseline_commit_sha: "f".repeat(40),
      implementation_correction_attempted: false,
      implementation_correction_result: null,
      implementation_correction_error: null,
      branch_name: null,
      branch_commit_sha: null,
      branch_created_at: null,
      created_at: "2026-08-20T09:00:00Z",
      updated_at: "2026-08-20T09:02:10Z",
      ...overrides,
    };
  }

  it("shows an automatic-fix button when validation failed and none has been attempted", async () => {
    fetchJob.mockResolvedValue(readyJobWithValidation({}));
    correctValidation.mockResolvedValue({ job_id: "job-correction" });

    render(<JobStatusView jobId="job-correction" />);

    const fixButton = await screen.findByRole("button", {
      name: "Attempt automatic fix",
    });
    fireEvent.click(fixButton);

    await waitFor(() =>
      expect(correctValidation).toHaveBeenCalledWith("job-correction"),
    );
  });

  it("hides the fix button and the correction card entirely when only npm install failed", async () => {
    fetchJob.mockResolvedValue(
      readyJobWithValidation({
        validation_results: [
          {
            name: "npm install",
            command: "npm install",
            status: "FAILED",
            summary: "Validation command failed with exit code 1.",
            output_excerpt: "npm ERR! network request to registry failed",
          },
        ],
      }),
    );

    render(<JobStatusView jobId="job-correction" />);

    await screen.findByText("Validation command failed with exit code 1.");
    expect(screen.queryByText("Validation correction")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Attempt automatic fix" }),
    ).not.toBeInTheDocument();
  });

  it("hides the fix button and shows the correction summary and changed files once already attempted", async () => {
    fetchJob.mockResolvedValue(
      readyJobWithValidation({
        implementation_correction_attempted: true,
        implementation_correction_result: {
          summary: "Fixed the lint failure.",
          changed_files: [
            {
              path: "fix.py",
              action: "modify",
              rationale: "Removed the undefined name.",
            },
          ],
          warnings: [],
          follow_up_questions: [],
        },
        validation_results: [
          {
            name: "ruff",
            command: "ruff check .",
            status: "PASSED",
            summary: "ok",
            output_excerpt: null,
          },
        ],
      }),
    );

    render(<JobStatusView jobId="job-correction" />);

    await screen.findByText("Fixed the lint failure.");
    expect(screen.getByText("fix.py")).toBeInTheDocument();
    expect(screen.getByText("Removed the undefined name.")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Attempt automatic fix" }),
    ).not.toBeInTheDocument();
  });

  it("shows the correction error banner when the fix attempt itself failed", async () => {
    fetchJob.mockResolvedValue(
      readyJobWithValidation({
        implementation_correction_attempted: true,
        implementation_correction_error: {
          code: "BUDGET_EXCEEDED",
          message:
            "The implementation agent exceeded its run budget before finishing.",
          stage: "CORRECTING",
        },
      }),
    );

    render(<JobStatusView jobId="job-correction" />);

    await screen.findByText(
      "The implementation agent exceeded its run budget before finishing.",
    );
    expect(
      screen.queryByRole("button", { name: "Attempt automatic fix" }),
    ).not.toBeInTheDocument();
  });

  it("creates a branch with the submitted name and message", async () => {
    fetchJob.mockResolvedValue(
      readyJobWithValidation({ validation_results: [] }),
    );
    createBranch.mockResolvedValue({
      branch_name: "custom/my-branch",
      commit_sha: "a".repeat(40),
    });

    render(<JobStatusView jobId="job-correction" />);

    const branchInput = await screen.findByPlaceholderText(
      "jira2pullreq/KAN-31",
    );
    fireEvent.change(branchInput, { target: { value: "custom/my-branch" } });
    fireEvent.click(screen.getByRole("button", { name: "Create branch" }));

    await waitFor(() =>
      expect(createBranch).toHaveBeenCalledWith("job-correction", {
        branch_name: "custom/my-branch",
        commit_message: "",
      }),
    );
  });

  it("shows the created branch and hides the form once a branch exists", async () => {
    fetchJob.mockResolvedValue(
      readyJobWithValidation({
        validation_results: [],
        branch_name: "jira2pullreq/KAN-31",
        branch_commit_sha: "b".repeat(40),
        branch_created_at: "2026-08-20T09:03:00Z",
      }),
    );

    render(<JobStatusView jobId="job-correction" />);

    await screen.findByText("jira2pullreq/KAN-31");
    expect(
      screen.queryByRole("button", { name: "Create branch" }),
    ).not.toBeInTheDocument();
  });

  it("maps a LOCAL_FOLDER source to repoMode local in the retry draft", async () => {
    sessionStorage.clear();
    fetchJob.mockResolvedValue({
      id: "job-folder-failed",
      ticket_key: "KAN-30",
      repo_url: "D:\\repos\\plain-folder",
      planning_notes: null,
      state: "FAILED",
      error: {
        code: "TICKET_NOT_FOUND",
        message: "That Jira ticket could not be found (or is not visible).",
        stage: "FETCHING_TICKET",
      },
      repo_info: {
        source_kind: "LOCAL_FOLDER",
        branch: null,
        commit_sha: "d".repeat(40),
        origin_url: null,
        is_dirty: false,
        local_path: "D:\\repos\\plain-folder",
      },
      workspace_path: "D:\\workdir\\job-folder-failed\\repo",
      plan: null,
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-08-05T17:54:02Z",
      updated_at: "2026-08-05T17:57:53Z",
    });

    render(<JobStatusView jobId="job-folder-failed" />);

    const retryButton = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(retryButton);

    expect(
      JSON.parse(sessionStorage.getItem("jira2pullreq:retry-draft")!),
    ).toEqual({
      ticket: "KAN-30",
      repo: "D:\\repos\\plain-folder",
      repoMode: "local",
      planningNotes: "",
      implementationClarifications: "",
    });
  });

  it("clears the ticket and flags the document source when retrying a DOCUMENT job", async () => {
    sessionStorage.clear();
    fetchJob.mockResolvedValue({
      id: "job-doc-failed",
      ticket_key: "DOC-A1B2C3D4",
      requirement_source: "DOCUMENT",
      requirement_document_name: "requirements.pdf",
      repo_url: "https://github.com/acme/repo.git",
      planning_notes: null,
      state: "FAILED",
      error: {
        code: "DOCUMENT_EMPTY",
        message: "The uploaded requirement document has no extractable text.",
        stage: "FETCHING_TICKET",
      },
      repo_info: {
        source_kind: "REMOTE",
        branch: "main",
        commit_sha: "e".repeat(40),
        origin_url: "https://github.com/acme/repo.git",
        is_dirty: false,
        local_path: null,
      },
      workspace_path: "D:\\workdir\\job-doc-failed\\repo",
      plan: null,
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-08-05T17:54:02Z",
      updated_at: "2026-08-05T17:57:53Z",
    });

    render(<JobStatusView jobId="job-doc-failed" />);

    const retryButton = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(retryButton);

    expect(
      JSON.parse(sessionStorage.getItem("jira2pullreq:retry-draft")!),
    ).toEqual({
      ticket: "",
      repo: "https://github.com/acme/repo.git",
      repoMode: "remote",
      planningNotes: "",
      requirementSource: "DOCUMENT",
      requirementDocumentName: "requirements.pdf",
      implementationClarifications: "",
    });
  });

  it("does not show a Retry button for a job that has not failed", async () => {
    fetchJob.mockResolvedValue({
      id: "job-remote",
      ticket_key: "PROJ-1",
      repo_url: "git@github.com:acme/repo.git",
      planning_notes: null,
      state: "PLAN_READY",
      error: null,
      repo_info: {
        source_kind: "REMOTE",
        branch: "main",
        commit_sha: "b".repeat(40),
        origin_url: "https://github.com/acme/repo.git",
        is_dirty: false,
        local_path: null,
      },
      workspace_path: "D:\\workdir\\job-remote\\repo",
      plan: {
        schema_version: 2,
        summary: "Do the thing.",
        ticket_type: "feature",
        estimated_story_points: 3,
        complexity_level: "medium",
        impacted_files: [{ path: "a.py", reason: "Entry point" }],
        proposed_changes: [
          { file: "a.py", action: "modify", description: "Apply the change." },
        ],
        test_strategy: "Run unit tests.",
        risks: [],
        open_questions: [],
      },
      usage: null,
      implementation_usage: null,
      implementation_result: null,
      implementation_diff: null,
      validation_results: [],
      implementation_clarifications: null,
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
    });

    render(<JobStatusView jobId="job-remote" />);

    await screen.findByRole("button", { name: /Approve and Implement/i });
    expect(
      screen.queryByRole("button", { name: "Retry" }),
    ).not.toBeInTheDocument();
  });
});
