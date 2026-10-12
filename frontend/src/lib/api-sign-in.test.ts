import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchSession } from "@/lib/api";

// The SSO proxy (deploy/vm nginx) answers an API call whose sign-in expired
// with 401 + X-Sign-In-Required. A plain 401 from the backend (dev stack, no
// proxy) must stay an ordinary error.
describe("API calls after the SSO sign-in expires", () => {
  const realLocation = window.location;
  const assign = vi.fn();

  function stubLocation(pathname: string) {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...realLocation,
        origin: "https://app.example.com",
        protocol: "https:",
        hostname: "app.example.com",
        port: "",
        pathname,
        search: "",
        assign,
      },
    });
  }

  function unauthorized(headers: Record<string, string>) {
    return new Response(
      JSON.stringify({
        error: { code: "UNAUTHENTICATED", message: "Sign in again." },
      }),
      {
        status: 401,
        headers: { "Content-Type": "application/json", ...headers },
      },
    );
  }

  beforeEach(() => {
    assign.mockReset();
  });

  afterEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: realLocation,
    });
    vi.unstubAllGlobals();
  });

  it("sends the browser to the sign-in page and back to where it was", async () => {
    stubLocation("/jobs/abc");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(unauthorized({ "X-Sign-In-Required": "1" })),
    );

    let settled = false;
    void fetchSession().finally(() => {
      settled = true;
    });

    await vi.waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/signin?expired=1&rd=%2Fjobs%2Fabc"),
    );
    // Callers never see an error flash while the page navigates away.
    await Promise.resolve();
    expect(settled).toBe(false);
  });

  it("leaves a plain backend 401 as an ordinary error", async () => {
    stubLocation("/jobs/abc");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(unauthorized({})));

    await expect(fetchSession()).rejects.toMatchObject({
      code: "UNAUTHENTICATED",
      status: 401,
    });
    expect(assign).not.toHaveBeenCalled();
  });
});
