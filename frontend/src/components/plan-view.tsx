import type { AgentUsage } from "@/lib/api";
import type { Plan } from "@/lib/plan.gen";

function currency(value: number | null): string {
  if (value === null) {
    return "Not recorded";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function PlanView(props: {
  plan: Plan;
  usage: AgentUsage | null;
  planningNotes?: string | null;
}) {
  const { plan, usage, planningNotes } = props;

  return (
    <section className="plan-layout">
      <div className="panel">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Plan review</span>
            <h2>Structured implementation plan</h2>
          </div>
          <span className="pill">Schema v{plan.schema_version}</span>
        </div>

        <div className="stack">
          <div>
            <span className="meta-label">Summary</span>
            <p>{plan.summary}</p>
          </div>
          {planningNotes ? (
            <div>
              <span className="meta-label">
                Technical considerations you provided
              </span>
              <p className="output-block">{planningNotes}</p>
            </div>
          ) : null}
          <div>
            <span className="meta-label">Ticket type</span>
            <p className="cap">{plan.ticket_type}</p>
          </div>
          <div>
            <span className="meta-label">Test strategy</span>
            <p>{plan.test_strategy}</p>
          </div>
        </div>
      </div>

      <div className="plan-columns">
        <section className="panel">
          <h3>Impacted files</h3>
          <ul className="content-list">
            {plan.impacted_files.map((file) => (
              <li key={`${file.path}-${file.reason}`}>
                <code>{file.path}</code>
                <p>{file.reason}</p>
              </li>
            ))}
          </ul>
        </section>

        <section className="panel">
          <h3>Proposed changes</h3>
          <ul className="content-list">
            {plan.proposed_changes.map((change) => (
              <li key={`${change.file}-${change.action}-${change.description}`}>
                <div className="change-header">
                  <code>{change.file}</code>
                  <span className="pill cap">{change.action}</span>
                </div>
                <p>{change.description}</p>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <div className="plan-columns">
        <section className="panel">
          <h3>Risks</h3>
          {plan.risks.length > 0 ? (
            <ul className="bullet-list">
              {plan.risks.map((risk) => (
                <li key={risk}>{risk}</li>
              ))}
            </ul>
          ) : (
            <p className="meta-muted">No explicit risks were recorded.</p>
          )}
        </section>

        <section className="panel">
          <h3>Open questions</h3>
          {plan.open_questions.length > 0 ? (
            <ul className="bullet-list">
              {plan.open_questions.map((question) => (
                <li key={question}>{question}</li>
              ))}
            </ul>
          ) : (
            <p className="meta-muted">No open questions were recorded.</p>
          )}
        </section>
      </div>

      <section className="panel">
        <h3>Agent usage</h3>
        <div className="summary-grid">
          <div className="summary-card">
            <span className="meta-label">Input tokens</span>
            <strong>{usage?.input_tokens ?? "Not recorded"}</strong>
          </div>
          <div className="summary-card">
            <span className="meta-label">Output tokens</span>
            <strong>{usage?.output_tokens ?? "Not recorded"}</strong>
          </div>
          <div className="summary-card">
            <span className="meta-label">Turns</span>
            <strong>{usage?.num_turns ?? "Not recorded"}</strong>
          </div>
          <div className="summary-card">
            <span className="meta-label">Duration</span>
            <strong>
              {usage ? `${usage.duration_seconds.toFixed(1)}s` : "Not recorded"}
            </strong>
          </div>
          <div className="summary-card">
            <span className="meta-label">Planning cost</span>
            <strong>{currency(usage?.total_cost_usd ?? null)}</strong>
          </div>
        </div>
      </section>
    </section>
  );
}
