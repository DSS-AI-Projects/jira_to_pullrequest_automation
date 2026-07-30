import { afterEach, describe, expect, it, vi } from "vitest";

import { defaultApiBaseUrl, implementJob } from "@/lib/api";

describe("defaultApiBaseUrl", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("uses the configured API base URL when provided", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "https://api.example.com/");

    expect(defaultApiBaseUrl()).toBe("https://api.example.com");
  });

  it("targets backend port 8000 for local frontend development", () => {
    expect(
      defaultApiBaseUrl({
        origin: "http://127.0.0.1:3000",
        protocol: "http:",
        hostname: "127.0.0.1",
        port: "3000",
      }),
    ).toBe("http://127.0.0.1:8000");
  });

  it("uses same-origin when served behind a shared deployment proxy", () => {
    expect(
      defaultApiBaseUrl({
        origin: "https://app.example.com",
        protocol: "https:",
        hostname: "app.example.com",
        port: "",
      }),
    ).toBe("https://app.example.com");
  });
});

describe("implementJob", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch() {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ job_id: "job-1" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("sends no body when clarifications are omitted", async () => {
    const fetchMock = stubFetch();

    await implementJob("job-1");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeUndefined();
    expect(init.headers).not.toHaveProperty("Content-Type");
  });

  it("sends no body when clarifications are blank", async () => {
    const fetchMock = stubFetch();

    await implementJob("job-1", { clarifications: "   " });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeUndefined();
  });

  it("sends the clarifications as a JSON body when present", async () => {
    const fetchMock = stubFetch();

    await implementJob("job-1", {
      clarifications: "Use British spelling.",
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBe(
      JSON.stringify({ clarifications: "Use British spelling." }),
    );
    expect(init.headers).toMatchObject({ "Content-Type": "application/json" });
  });
});
