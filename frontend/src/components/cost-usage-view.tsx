"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  ApiError,
  fetchCostSummary,
  isAbortError,
  type CostSummaryResponse,
} from "@/lib/api";

function currency(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function CostUsageView() {
  const [summary, setSummary] = useState<CostSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setForbidden(false);
    fetchCostSummary(controller.signal)
      .then(setSummary)
      .catch((loadError: unknown) => {
        if (isAbortError(loadError)) {
          return;
        }
        if (loadError instanceof ApiError && loadError.code === "FORBIDDEN") {
          setForbidden(true);
          return;
        }
        setError(
          loadError instanceof Error
            ? loadError.message
            : "Could not load cost usage.",
        );
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, []);

  if (loading) {
    return (
      <section className="panel">
        <p className="meta-muted">Loading cost usage...</p>
      </section>
    );
  }

  if (forbidden) {
    return (
      <section className="panel">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Admin</span>
            <h1>Cost usage</h1>
          </div>
          <Link className="secondary-link" href="/">
            New job
          </Link>
        </div>
        <p className="meta-muted">
          Admins only. Sign in with an admin account to view per-user cost
          usage.
        </p>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Admin</span>
          <h1>Cost usage by user</h1>
          <p>Planning and implementation spend, grouped by job owner.</p>
        </div>
        <Link className="secondary-link" href="/">
          New job
        </Link>
      </div>

      {error ? <p className="banner banner-error">{error}</p> : null}

      {summary && summary.owners.length > 0 ? (
        <>
          <ul className="content-list">
            {summary.owners.map((owner) => (
              <li key={owner.user_id ?? "no-owner"}>
                <div className="change-header">
                  <strong>{owner.display_name ?? "No owner"}</strong>
                  <span className="pill">{owner.job_count} jobs</span>
                </div>
                {owner.email ? (
                  <p className="meta-muted break-all">{owner.email}</p>
                ) : null}
                <div className="summary-grid">
                  <div className="summary-card">
                    <span className="meta-label">Planning cost</span>
                    <strong>{currency(owner.planning_cost_usd)}</strong>
                  </div>
                  <div className="summary-card">
                    <span className="meta-label">Implementation cost</span>
                    <strong>{currency(owner.implementation_cost_usd)}</strong>
                  </div>
                  <div className="summary-card">
                    <span className="meta-label">Total cost</span>
                    <strong>{currency(owner.total_cost_usd)}</strong>
                  </div>
                </div>
              </li>
            ))}
          </ul>
          <div className="summary-card">
            <span className="meta-label">Grand total across all users</span>
            <strong>{currency(summary.grand_total_usd)}</strong>
          </div>
        </>
      ) : (
        <p className="meta-muted">No job cost data recorded yet.</p>
      )}
    </section>
  );
}
