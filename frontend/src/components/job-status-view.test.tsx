import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobStatusView } from "@/components/job-status-view";

const { fetchJob, implementJob } = vi.hoisted(() => ({
  fetchJob: vi.fn(),
  implementJob: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => <a {...props} />,
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
  });

  it("shows approve button for local plan-ready jobs and renders implementation results", async () => {
    fetchJob
      .mockResolvedValueOnce({
        id: "job-123",
        ticket_key: "KAN-25",
        repo_url: "D:\\workspaces\\hello-world-debug",
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
          schema_version: 1,
          summary: "Update the greeting text.",
          ticket_type: "chore",
          impacted_files: [{ path: "README", reason: "Contains the greeting" }],
          proposed_changes: [
            { file: "README", action: "modify", description: "Replace Hello World text." },
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
        validation_results: [],
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
          schema_version: 1,
          summary: "Update the greeting text.",
          ticket_type: "chore",
          impacted_files: [{ path: "README", reason: "Contains the greeting" }],
          proposed_changes: [
            { file: "README", action: "modify", description: "Replace Hello World text." },
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
        validation_results: [
          {
            name: "validation-profile",
            command: "",
            status: "SKIPPED",
            summary: "No recognized validation profile was detected for this repository.",
            output_excerpt: null,
          },
        ],
        implementation_approved_at: "2026-07-17T00:02:00Z",
        implementation_started_at: "2026-07-17T00:02:01Z",
        implementation_finished_at: "2026-07-17T00:02:10Z",
        created_at: "2026-07-17T00:00:00Z",
        updated_at: "2026-07-17T00:02:10Z",
      });
    implementJob.mockResolvedValue({ job_id: "job-123" });

    render(<JobStatusView jobId="job-123" />);

    const button = await screen.findByRole("button", {
      name: /Approve and Implement/i,
    });
    fireEvent.click(button);

    await waitFor(() => expect(implementJob).toHaveBeenCalledWith("job-123"));
    await screen.findByText(/Updated README greeting/i);
    expect(screen.getByText(/validation results/i)).toBeInTheDocument();
    expect(screen.getByText(/Confirm punctuation/i)).toBeInTheDocument();
    expect(screen.getByText(/Implementation workspace/i)).toBeInTheDocument();
    expect(screen.getByText(/D:\\workdir\\job-123\\repo/i)).toBeInTheDocument();
    expect(screen.getByText(/\$0.15/)).toBeInTheDocument();
  });

  it("explains when implementation approval is unavailable for remote jobs", async () => {
    fetchJob.mockResolvedValue({
      id: "job-remote",
      ticket_key: "PROJ-1",
      repo_url: "git@github.com:acme/repo.git",
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
        schema_version: 1,
        summary: "Do the thing.",
        ticket_type: "feature",
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
      validation_results: [],
      implementation_approved_at: null,
      implementation_started_at: null,
      implementation_finished_at: null,
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
    });

    render(<JobStatusView jobId="job-remote" />);

    await screen.findByText(/Implementation approval is available only for local repository jobs/i);
    expect(
      screen.queryByRole("button", { name: /Approve and Implement/i }),
    ).not.toBeInTheDocument();
  });

  it("renders legacy implementation-ready jobs that omit validation results", async () => {
    fetchJob.mockResolvedValue({
      id: "job-legacy",
      ticket_key: "KAN-25",
      repo_url: "D:\\work\\2026\\AI\\Trae\\Hello-World",
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
        schema_version: 1,
        summary: "Update the greeting text.",
        ticket_type: "chore",
        impacted_files: [{ path: "README", reason: "Contains the greeting" }],
        proposed_changes: [
          { file: "README", action: "modify", description: "Replace Hello World text." },
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
      validation_results: undefined,
      implementation_approved_at: "2026-07-17T00:02:00Z",
      implementation_started_at: "2026-07-17T00:02:01Z",
      implementation_finished_at: "2026-07-17T00:02:10Z",
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:02:10Z",
    });

    render(<JobStatusView jobId="job-legacy" />);

    await screen.findByText(/Updated README greeting/i);
    expect(screen.getByText(/Validation results/i)).toBeInTheDocument();
    expect(screen.getByText(/Check the filename mismatch/i)).toBeInTheDocument();
  });
});
