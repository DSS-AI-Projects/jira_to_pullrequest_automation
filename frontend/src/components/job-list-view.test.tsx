import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobListView } from "@/components/job-list-view";

const { fetchJobs } = vi.hoisted(() => ({
  fetchJobs: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props} />
  ),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchJobs,
  };
});

describe("JobListView", () => {
  beforeEach(() => {
    fetchJobs.mockReset();
  });

  it("renders jobs with state and a link to the detail page", async () => {
    fetchJobs.mockResolvedValue({
      jobs: [
        {
          id: "job-1",
          ticket_key: "PROJ-1",
          repo_url: "https://github.com/acme/repo",
          state: "PLAN_READY",
          created_at: "2026-07-17T00:00:00Z",
          updated_at: "2026-07-17T00:00:00Z",
          error_code: null,
        },
      ],
      next_cursor: null,
    });

    render(<JobListView />);

    const link = await screen.findByRole("link", { name: "PROJ-1" });
    expect(link).toHaveAttribute("href", "/jobs/job-1");
    expect(screen.getByText("Plan ready")).toBeInTheDocument();
    expect(
      screen.getByText("https://github.com/acme/repo"),
    ).toBeInTheDocument();
  });

  it("always shows a New job link, including in the empty state", async () => {
    fetchJobs.mockResolvedValue({ jobs: [], next_cursor: null });

    render(<JobListView />);

    await screen.findByText(/No jobs yet/i);
    expect(screen.getByRole("link", { name: "New job" })).toHaveAttribute(
      "href",
      "/",
    );
  });

  it("loads the next page and appends jobs when Load more is clicked", async () => {
    fetchJobs
      .mockResolvedValueOnce({
        jobs: [
          {
            id: "job-1",
            ticket_key: "PROJ-1",
            repo_url: "https://github.com/acme/repo",
            state: "PLAN_READY",
            created_at: "2026-07-17T00:02:00Z",
            updated_at: "2026-07-17T00:02:00Z",
            error_code: null,
          },
        ],
        next_cursor: "2026-07-17T00:02:00Z",
      })
      .mockResolvedValueOnce({
        jobs: [
          {
            id: "job-2",
            ticket_key: "PROJ-2",
            repo_url: "https://github.com/acme/repo",
            state: "FAILED",
            created_at: "2026-07-17T00:00:00Z",
            updated_at: "2026-07-17T00:00:00Z",
            error_code: "TICKET_NOT_FOUND",
          },
        ],
        next_cursor: null,
      });

    render(<JobListView />);

    await screen.findByRole("link", { name: "PROJ-1" });
    fireEvent.click(screen.getByRole("button", { name: /Load more/i }));

    await screen.findByRole("link", { name: "PROJ-2" });
    expect(screen.getByRole("link", { name: "PROJ-1" })).toBeInTheDocument();
    await waitFor(() =>
      expect(fetchJobs).toHaveBeenLastCalledWith({
        limit: 20,
        before: "2026-07-17T00:02:00Z",
      }),
    );
    expect(screen.getByText(/TICKET_NOT_FOUND/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Load more/i }),
    ).not.toBeInTheDocument();
  });
});
