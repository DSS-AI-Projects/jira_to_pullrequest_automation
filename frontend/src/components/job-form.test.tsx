import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobForm } from "@/components/job-form";

const { push, createJob, fetchRepos } = vi.hoisted(() => ({
  push: vi.fn(),
  createJob: vi.fn(),
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
    fetchRepos,
  };
});

describe("JobForm", () => {
  beforeEach(() => {
    push.mockReset();
    createJob.mockReset();
    fetchRepos.mockReset();
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
      },
    });
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
    fireEvent.click(
      screen.getByRole("button", { name: /Generate implementation plan/i }),
    );

    await waitFor(() =>
      expect(createJob).toHaveBeenCalledWith({
        ticket: "PROJ-42",
        repo: "hello-world-sample",
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-123");
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
      },
    });
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
      },
    });
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
      }),
    );
    expect(push).toHaveBeenCalledWith("/jobs/job-local");
  });
});
