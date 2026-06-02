"use client";

import { useEffect, useState } from "react";
import { fetchAIInsights } from "@/lib/api";
import type {
  Physician,
  SelectedSite,
  AIInsightsData,
} from "@/types/physician";

interface Props {
  physician: Physician;
  site: SelectedSite;
  onBack: () => void;
}

export default function AIInsightsView({ physician, site, onBack }: Props) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [insights, setInsights] = useState<AIInsightsData | null>(null);
  const [citationTooltip, setCitationTooltip] = useState(false);

  useEffect(() => {
    async function loadInsights() {
      try {
        setLoading(true);
        const data = await fetchAIInsights({
          npi:       physician.npi,
          name:      physician.name,
          specialty: physician.taxonomy_desc || "",
          disease:   site.condition || "",
        });
        setInsights(data);
      } catch (err) {
        console.error(err);
        setError("Failed to load AI physician insights.");
      } finally {
        setLoading(false);
      }
    }
    loadInsights();
  }, [physician, site]);

  if (loading) {
    return (
      <div className="aiv-loading">
        <div className="aiv-loader-card">
          <h2>Loading AI Physician Insights...</h2>
          <p>Please wait while AI enriches physician data.</p>
        </div>
        <style jsx>{`
          .aiv-loading { padding: 32px; }
          .aiv-loader-card {
            background: white; border-radius: 20px; padding: 40px;
            text-align: center; box-shadow: 0 4px 16px rgba(0,0,0,0.06);
          }
        `}</style>
      </div>
    );
  }

  if (error || !insights) {
    return (
      <div className="aiv-loading">
        <div className="aiv-loader-card">
          <h2>Unable to Load Insights</h2>
          <p>{error}</p>
          <button onClick={onBack}>← Back</button>
        </div>
        <style jsx>{`
          .aiv-loading { padding: 32px; }
          .aiv-loader-card {
            background: white; border-radius: 20px; padding: 40px;
            text-align: center; box-shadow: 0 4px 16px rgba(0,0,0,0.06);
          }
          button {
            margin-top: 20px; border: none; background: #2563eb;
            color: white; padding: 12px 18px; border-radius: 10px; cursor: pointer;
          }
        `}</style>
      </div>
    );
  }

  // Board certifications from specialty string
  const boardCerts = (physician.taxonomy_desc || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  return (
    <div className="aiv-page">

      {/* Header */}
      <div className="aiv-header">
        <button className="aiv-back-btn" onClick={onBack}>
          ← Back to Physician
        </button>

        <div className="aiv-profile-card">
          <div className="aiv-avatar">{physician.name?.charAt(0)}</div>
          <div className="aiv-profile-info">
            <h1>{physician.name}</h1>
            <p className="aiv-specialty">{physician.taxonomy_desc}</p>
            <span className="aiv-npi">NPI: {physician.npi}</span>

            {/* Board Certifications */}
            {boardCerts.length > 0 && (
              <div className="aiv-certs">
                <span className="aiv-certs-label">Board Certified In:</span>
                <div className="aiv-certs-tags">
                  {boardCerts.map((cert, idx) => (
                    <span className="aiv-cert-tag" key={idx}>{cert}</span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* AI Summary */}
      <section className="aiv-card">
        <h2>✨ AI Summary</h2>
        <p className="aiv-summary">
          {insights.ai_summary || "No AI summary available."}
        </p>
      </section>

      {/* Metrics — Publications | Total Citations (H-Index removed) */}
      <section className="aiv-metrics-grid">

        {/* Publications */}
        <div className="aiv-metric-card">
          <h3>Publications</h3>
          <p>{insights.publication_count || 0}</p>
        </div>

        {/* Total Citations with tooltip */}
        <div className="aiv-metric-card aiv-metric-citations">
          <h3>
            Total Citations
            <span
              className="aiv-tooltip-trigger"
              onMouseEnter={() => setCitationTooltip(true)}
              onMouseLeave={() => setCitationTooltip(false)}
              onClick={() => setCitationTooltip(!citationTooltip)}
            >
              ?
            </span>
          </h3>
          <p>{insights.total_citations || 0}</p>

          {/* Tooltip rendered inside metric card for correct positioning */}
          {citationTooltip && (
            <div className="aiv-tooltip-box">
              The total number of times this physician's published research
              has been cited by other medical papers worldwide.
              More citations = more influential research in the medical community.
            </div>
          )}
        </div>

      </section>

      {/* Research Areas */}
      <section className="aiv-card">
        <h2>📚 Research Areas</h2>
        <div className="aiv-tags">
          {insights.research_areas?.length ? (
            insights.research_areas.map((area, idx) => (
              <span className="aiv-tag" key={idx}>{area}</span>
            ))
          ) : (
            <p>No research areas found.</p>
          )}
        </div>
      </section>

      {/* Publications */}
      <section className="aiv-card">
        <h2>📄 Verified Publications</h2>
        <div className="aiv-publications">
          {insights.publications?.length ? (
            insights.publications.map((pub, idx) => (
              <div className="aiv-publication-card" key={idx}>
                <div className="aiv-publication-content">
                  <h3>{pub.title}</h3>
                  <p>{pub.source} • {pub.year}</p>
                </div>
                <a
                  href={pub.best_url || pub.doi_url || pub.pubmed_url || pub.semantic_scholar_url || pub.url || "#"}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="aiv-publication-btn"
                >
                  Open Publication
                </a>
              </div>
            ))
          ) : (
            <p>No verified publications found.</p>
          )}
        </div>
      </section>

      {/* Awards */}
      <section className="aiv-card">
        <h2>🏆 Awards & Recognition</h2>
        {insights.awards?.length ? (
          <ul className="aiv-awards">
            {insights.awards.map((award, idx) => (
              <li key={idx}>{award}</li>
            ))}
          </ul>
        ) : (
          <p>No awards data available currently.</p>
        )}
      </section>

      <style jsx>{`
        .aiv-page {
          padding: 24px; overflow-y: auto; height: 100%; background: #f5f7fb;
        }

        .aiv-header { margin-bottom: 24px; }

        .aiv-back-btn {
          border: none; background: white; padding: 10px 16px;
          border-radius: 10px; cursor: pointer; font-weight: 600; margin-bottom: 20px;
        }

        .aiv-profile-card {
          background: white; border-radius: 20px; padding: 24px;
          display: flex; gap: 20px; align-items: flex-start;
          box-shadow: 0 4px 16px rgba(0,0,0,0.06);
        }

        .aiv-avatar {
          width: 72px; height: 72px; border-radius: 50%; background: #2563eb;
          color: white; display: flex; align-items: center; justify-content: center;
          font-size: 28px; font-weight: 700; flex-shrink: 0;
        }

        .aiv-profile-info { display: flex; flex-direction: column; gap: 4px; }
        .aiv-profile-info h1 { font-size: 20px; font-weight: 700; margin: 0; }
        .aiv-specialty { color: #6b7280; margin: 0; font-size: 14px; }
        .aiv-npi { color: #9ca3af; font-size: 13px; }

        /* Board Certifications */
        .aiv-certs { margin-top: 10px; }
        .aiv-certs-label {
          font-size: 12px; font-weight: 600; color: #6b7280;
          text-transform: uppercase; letter-spacing: 0.05em;
        }
        .aiv-certs-tags {
          display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px;
        }
        .aiv-cert-tag {
          background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0;
          padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
        }

        .aiv-card {
          background: white; border-radius: 20px; padding: 24px;
          margin-bottom: 24px; box-shadow: 0 4px 16px rgba(0,0,0,0.06);
        }
        .aiv-summary { line-height: 1.8; color: #374151; }

        /* Metrics */
        .aiv-metrics-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 20px; margin-bottom: 24px;
        }

        .aiv-metric-card {
          background: white; border-radius: 20px; padding: 24px;
          text-align: center; box-shadow: 0 4px 16px rgba(0,0,0,0.06);
          position: relative; overflow: visible;
        }

        .aiv-metric-card h3 {
          font-size: 14px; color: #6b7280; font-weight: 600;
          display: flex; align-items: center; justify-content: center; gap: 6px;
          margin: 0;
        }

        .aiv-metric-card p {
          font-size: 32px; font-weight: 700; color: #2563eb; margin-top: 10px;
          margin-bottom: 0;
        }

        /* Tooltip trigger — ? button */
        .aiv-tooltip-trigger {
          display: inline-flex; align-items: center; justify-content: center;
          width: 18px; height: 18px; border-radius: 50%;
          background: #dbeafe; color: #1d4ed8;
          font-size: 11px; font-weight: 700;
          cursor: pointer; flex-shrink: 0;
          user-select: none;
        }

        /* Tooltip box — anchored to metric card */
        .aiv-tooltip-box {
          position: absolute;
          top: calc(100% + 10px);
          left: 50%;
          transform: translateX(-50%);
          background: #1e293b;
          color: white;
          font-size: 13px;
          font-weight: 400;
          padding: 14px 16px;
          border-radius: 12px;
          width: 270px;
          line-height: 1.7;
          z-index: 9999;
          text-align: left;
          box-shadow: 0 12px 40px rgba(0,0,0,0.3);
          pointer-events: none;
        }

        /* Arrow pointing up */
        .aiv-tooltip-box::before {
          content: "";
          position: absolute;
          top: -7px;
          left: 50%;
          transform: translateX(-50%);
          border-width: 0 7px 7px 7px;
          border-style: solid;
          border-color: transparent transparent #1e293b transparent;
        }

        /* Tags */
        .aiv-tags { display: flex; flex-wrap: wrap; gap: 12px; }
        .aiv-tag {
          background: #dbeafe; color: #1d4ed8;
          padding: 10px 14px; border-radius: 999px;
          font-size: 14px; font-weight: 600;
        }

        /* Publications */
        .aiv-publications { display: flex; flex-direction: column; gap: 16px; }
        .aiv-publication-card {
          border: 1px solid #e5e7eb; border-radius: 16px; padding: 18px;
          display: flex; justify-content: space-between;
          align-items: center; gap: 20px;
        }
        .aiv-publication-content h3 {
          margin-bottom: 8px; font-size: 16px; color: #111827;
        }
        .aiv-publication-content p { color: #6b7280; font-size: 14px; }
        .aiv-publication-btn {
          background: #2563eb; color: white; text-decoration: none;
          padding: 10px 16px; border-radius: 10px;
          font-weight: 600; white-space: nowrap;
        }

        .aiv-awards { padding-left: 20px; }

        @media (max-width: 768px) {
          .aiv-page { padding: 16px; }
          .aiv-profile-card { flex-direction: column; align-items: flex-start; }
          .aiv-publication-card { flex-direction: column; align-items: flex-start; }
          .aiv-tooltip-box { width: 220px; left: 0; transform: none; }
        }
      `}</style>
    </div>
  );
}
