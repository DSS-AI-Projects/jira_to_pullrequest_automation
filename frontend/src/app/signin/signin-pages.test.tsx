import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import SignedOutPage from "@/app/signin/signed-out/page";
import SignInPage from "@/app/signin/page";

describe("sign-in pages", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("offers the provider sign-in, returning to the requested page", async () => {
    vi.stubEnv("SSO_PROVIDER_LABEL", "Microsoft");

    render(
      await SignInPage({ searchParams: Promise.resolve({ rd: "/jobs/abc" }) }),
    );

    expect(
      screen.getByRole("link", { name: "Sign in with Microsoft" }),
    ).toHaveAttribute("href", "/oauth2/start?rd=%2Fjobs%2Fabc");
    expect(screen.queryByText(/sign-in has expired/i)).not.toBeInTheDocument();
    expect(screen.getByText(/never sees your password/i)).toBeInTheDocument();
  });

  it("explains an expired sign-in and never returns to another site", async () => {
    render(
      await SignInPage({
        searchParams: Promise.resolve({
          expired: "1",
          rd: "https://evil.example.com/",
        }),
      }),
    );

    expect(screen.getByText(/sign-in has expired/i)).toBeInTheDocument();
    // No label configured: a generic one, and the return path falls back home.
    expect(
      screen.getByRole("link", { name: "Sign in with your organisation" }),
    ).toHaveAttribute("href", "/oauth2/start?rd=%2F");
  });

  it("confirms sign-out and links back to sign-in", () => {
    vi.stubEnv("SSO_PROVIDER_LABEL", "Microsoft");

    render(<SignedOutPage />);

    expect(
      screen.getByRole("heading", { name: "You have signed out" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/your Microsoft account/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in again" })).toHaveAttribute(
      "href",
      "/signin",
    );
  });
});
