// Server-only: the sign-in pages are rendered per request, so the label comes
// from the frontend container's environment (deploy/vm sets "Microsoft")
// rather than being baked in at build time.
export function ssoProviderLabel(): string {
  // An empty value counts as unset, hence not `??`.
  const label = process.env.SSO_PROVIDER_LABEL?.trim();
  if (label) {
    return label;
  }
  return "your organisation";
}
