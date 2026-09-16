import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement scrollIntoView at all (not even as a no-op) — app
// code that calls it (e.g. focusStatusPanel in job-status-view.tsx) throws
// a real, uncaught TypeError under jsdom without this. Real browsers have
// always had it; this only fills the test-environment gap.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => undefined;
}
