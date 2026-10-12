// Single sign-on in front of the app (deploy/vm: nginx + oauth2-proxy +
// Entra ID). These pages are public; everything else is behind the proxy.

export const SIGN_IN_PATH = "/signin";
export const SIGNED_OUT_PATH = "/signin/signed-out";

// nginx answers an API call whose SSO sign-in has expired with 401 and this
// header, instead of passing it on to the backend.
export const SIGN_IN_REQUIRED_HEADER = "X-Sign-In-Required";

export function isPublicSignInPath(
  pathname: string | null | undefined,
): boolean {
  return (
    pathname === SIGN_IN_PATH || (pathname ?? "").startsWith(`${SIGN_IN_PATH}/`)
  );
}

/**
 * Where to send the user after sign-in: an in-app path only. Anything else
 * (another site, a protocol-relative `//host`, the sign-in or proxy pages
 * themselves) falls back to the home page, so the parameter can't be used as
 * an open redirect or a redirect loop. oauth2-proxy validates it again.
 */
export function safeReturnPath(raw: string | null | undefined): string {
  const value = (raw ?? "").trim();
  if (
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.startsWith("/\\") ||
    isPublicSignInPath(value.split(/[?#]/, 1)[0]) ||
    value.startsWith("/oauth2/")
  ) {
    return "/";
  }
  return value;
}

/** oauth2-proxy's start of the provider sign-in, returning to `returnPath`. */
export function ssoStartUrl(returnPath: string | null | undefined): string {
  return `/oauth2/start?rd=${encodeURIComponent(safeReturnPath(returnPath))}`;
}

/** The sign-in page, told the session expired and where to come back to. */
export function expiredSignInUrl(currentPath: string): string {
  return `${SIGN_IN_PATH}?expired=1&rd=${encodeURIComponent(safeReturnPath(currentPath))}`;
}
