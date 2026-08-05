import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CostUsageView } from "@/components/cost-usage-view";
import { ApiError } from "@/lib/api";

const { fetchCostSummary } = vi.hoisted(() => ({
  fetchCostSummary: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchCostSummary,
  };
});

describe("CostUsageView", () => {
  beforeEach(() => {
    fetchCostSummary.mockReset();
  });

  it("renders a per-user cost table for an admin", async () => {
    fetchCostSummary.mockResolvedValue({
      owners: [
        {
          user_id: "user-1",
          email: "worker@example.com",
          display_name: "Worker",
          job_count: 2,
          planning_cost_usd: 0.3,
          implementation_cost_usd: 0.5,
          total_cost_usd: 0.8,
        },
      ],
      grand_total_usd: 0.8,
    });

    render(<CostUsageView />);

    await screen.findByText("Worker");
    expect(screen.getByText("worker@example.com")).toBeInTheDocument();
    expect(screen.getByText("2 jobs")).toBeInTheDocument();
    expect(screen.getAllByText("$0.80")).toHaveLength(2); // total row + grand total
  });

  it("shows an admins-only message when the API returns FORBIDDEN", async () => {
    fetchCostSummary.mockRejectedValue(
      new ApiError(
        "You do not have access to that resource.",
        "FORBIDDEN",
        403,
      ),
    );

    render(<CostUsageView />);

    await screen.findByText(/Admins only/i);
  });
});
