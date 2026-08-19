import { JobForm } from "@/components/job-form";

export default function HomePage() {
  return (
    <main className="page-shell">
      <section className="hero-card home-hero">
        <div className="hero-copy">
          <span className="eyebrow">Milestone 1</span>
          <h1>Jira ticket to implementation plan</h1>
          <p className="lede">
            Submit a Jira ticket and either a repository identifier or an
            approved local repository path to generate a structured, reviewable
            implementation plan. This UI accepts identifiers only and never asks
            for secrets.
          </p>
        </div>
        <JobForm />
      </section>
    </main>
  );
}
