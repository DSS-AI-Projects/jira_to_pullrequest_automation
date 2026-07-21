import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthGate } from "@/components/auth-gate";

const { devLogin, fetchSession, logout } = vi.hoisted(() => ({
  devLogin: vi.fn(),
  fetchSession: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    devLogin,
    fetchSession,
    logout,
  };
});

describe("AuthGate", () => {
  beforeEach(() => {
    devLogin.mockReset();
    fetchSession.mockReset();
    logout.mockReset();
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
    await screen.findByText("App content");
    await screen.findByText("sam@example.com");
  });
});
