import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PlanView } from "@/components/plan-view";

describe("PlanView", () => {
  it("renders the structured plan content", () => {
    render(
      <PlanView
        plan={{
          schema_version: 1,
          summary: "Update the CLI to support verbose mode.",
          ticket_type: "feature",
          impacted_files: [
            {
              path: "src/cli.ts",
              reason: "The command-line parser needs a new verbose flag.",
            },
          ],
          proposed_changes: [
            {
              file: "src/cli.ts",
              action: "modify",
              description: "Add a --verbose option and wire it to logging.",
            },
          ],
          test_strategy:
            "Add unit coverage for argument parsing and logging behavior.",
          risks: ["Verbose mode may increase log noise for existing scripts."],
          open_questions: [
            "Should verbose mode also enable debug-level API logs?",
          ],
        }}
        usage={{
          input_tokens: 100,
          output_tokens: 25,
          total_cost_usd: 0.12,
          num_turns: 2,
          duration_seconds: 4.1,
        }}
      />,
    );

    expect(
      screen.getByText("Structured implementation plan"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Update the CLI to support verbose mode."),
    ).toBeInTheDocument();
    expect(screen.getAllByText("src/cli.ts")).toHaveLength(2);
    expect(
      screen.getByText(
        "Verbose mode may increase log noise for existing scripts.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("$0.12")).toBeInTheDocument();
  });
});
