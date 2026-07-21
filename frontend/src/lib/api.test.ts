import { afterEach, describe, expect, it, vi } from "vitest";

import { defaultApiBaseUrl } from "@/lib/api";

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
