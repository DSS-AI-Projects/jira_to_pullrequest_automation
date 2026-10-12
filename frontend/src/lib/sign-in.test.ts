import { describe, expect, it } from "vitest";

import {
  expiredSignInUrl,
  isPublicSignInPath,
  safeReturnPath,
  ssoStartUrl,
} from "@/lib/sign-in";

describe("sign-in helpers", () => {
  it("treats only the sign-in pages as public", () => {
    expect(isPublicSignInPath("/signin")).toBe(true);
    expect(isPublicSignInPath("/signin/signed-out")).toBe(true);
    expect(isPublicSignInPath("/signinx")).toBe(false);
    expect(isPublicSignInPath("/jobs")).toBe(false);
    expect(isPublicSignInPath(null)).toBe(false);
  });

  it("keeps in-app return paths, including their query", () => {
    expect(safeReturnPath("/jobs/abc")).toBe("/jobs/abc");
    expect(safeReturnPath("/jobs?cursor=2")).toBe("/jobs?cursor=2");
  });

  it.each([
    "https://evil.example.com/",
    "//evil.example.com/",
    "/\\evil.example.com",
    "javascript:alert(1)",
    "jobs",
    "/signin",
    "/signin/signed-out?x=1",
    "/oauth2/sign_out",
    "",
    null,
  ])("falls back to the home page for %s", (raw) => {
    expect(safeReturnPath(raw)).toBe("/");
  });

  it("builds the proxy sign-in and expired-session URLs", () => {
    expect(ssoStartUrl("/jobs/abc")).toBe("/oauth2/start?rd=%2Fjobs%2Fabc");
    expect(ssoStartUrl("https://evil.example.com")).toBe(
      "/oauth2/start?rd=%2F",
    );
    expect(expiredSignInUrl("/jobs/abc")).toBe(
      "/signin?expired=1&rd=%2Fjobs%2Fabc",
    );
  });
});
