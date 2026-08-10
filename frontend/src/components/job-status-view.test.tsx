import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobStatusView } from "@/components/job-status-view";

const { fetchJob, implementJob, push } = vi.hoisted(() => ({
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
    fetchJob,
    implementJob,
  };
});

describe("JobStatusView", () => {
  beforeEach(() => {
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

  it("explains when implementation approval is unavailable for remote jobs", async () => {
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

    await screen.findByText(
      /Implementation approval is available only for local repository jobs/i,
    );
    expect(
      screen.queryByRole("button", { name: /Approve and Implement/i }),
    ).not.toBeInTheDocument();
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
    });
    expect(push).toHaveBeenCalledWith("/");
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

    await screen.findByText(
      /Implementation approval is available only for local repository jobs/i,
    );
    expect(
      screen.queryByRole("button", { name: "Retry" }),
    ).not.toBeInTheDocument();
  });
});
