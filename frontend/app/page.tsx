'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { api, formatMoney, humanIssue, type DashboardData, type FeedbackPanel, type MetricsResponse, type PipelineStatus } from '../lib/api';

export default function Dashboard() {
  const [d, setD] = useState<DashboardData | null>(null);
  const [fb, setFb] = useState<FeedbackPanel | null>(null);
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [err, setErr] = useState('');
  const [running, setRunning] = useState(false);
  const [pipeStatus, setPipeStatus] = useState<PipelineStatus | null>(null);

  const load = () => {
    api.dashboard().then(setD).catch((e) => setErr(e.message));
    api.feedback().then(setFb).catch(() => setFb(null));
    api.metrics().then(setMetrics).catch(() => setMetrics(null));
  };

  useEffect(() => {
    load();
  }, []);

  const runPipeline = async () => {
    setRunning(true);
    setPipeStatus(null);
    try {
      await api.runPipeline();
      const poll = (): Promise<PipelineStatus> =>
        new Promise((resolve) => {
          const tick = () => {
            api.pipelineStatus().then((s) => {
              setPipeStatus(s);
              if (s.state === 'running') setTimeout(tick, 1500);
              else resolve(s);
            }).catch(() => setTimeout(tick, 3000));
          };
          tick();
        });
      await poll();
      load();
    } catch (e) {
      setPipeStatus({
        state: 'failed', stages: [], populated: false, total_cases: 0,
        error: e instanceof Error ? e.message : 'Pipeline failed',
      });
    } finally {
      setRunning(false);
    }
  };

  if (err) {
    return (
      <main className="main">
        <div className="card" style={{ maxWidth: 480, margin: '60px auto', textAlign: 'center' }}>
          <div style={{ fontSize: 32, marginBottom: 12, opacity: 0.4 }}>⚠</div>
          <h2 style={{ fontSize: 16, marginBottom: 6 }}>API Unavailable</h2>
          <p className="muted" style={{ marginBottom: 12, fontSize: 13 }}>
            Start the FastAPI backend on port 8000.
          </p>
          <code className="tag" style={{ fontSize: 11 }}>{err}</code>
        </div>
      </main>
    );
  }

  if (!d) {
    return (
      <main className="main">
        <div className="loading">Loading ReconAI</div>
      </main>
    );
  }

  const core = d.cases || {};
  const issues = d.issue_breakdown || [];
  const decisions = d.review_breakdown || [];
  const vendors = d.top_vendors || [];
  const totalCases = core.total || 0;

  return (
    <main className="main">
      {/* Header */}
      <div className="page-head">
        <div>
          <div className="eyebrow">Financial investigation platform</div>
          <h1 className="title">ReconAI Command Center</h1>
          <div className="subtitle">
            Reconcile, investigate, prioritize and review financial exceptions.
          </div>
        </div>
        <div className="btn-group">
          <button className="btn" onClick={runPipeline} disabled={running}>
            {running ? '⟳ Running…' : '▶ Run Pipeline'}
          </button>
          <Link className="btn primary" href="/cases">
            Open investigation queue →
          </Link>
        </div>
      </div>

      {/* Pipeline result (live status while/after running) */}
      {(running || pipeStatus) && (
        <div
          className="card fade-in"
          style={{
            marginBottom: 16,
            borderColor: pipeStatus?.state === 'failed' ? 'var(--danger-border)' : 'var(--success-border)',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <b style={{ color: pipeStatus?.state === 'failed' ? 'var(--danger)' : 'var(--success)', fontSize: 13 }}>
                Pipeline {pipeStatus?.state === 'running' ? 'running' : pipeStatus?.state === 'failed' ? 'failed' : 'completed'}
              </b>
              {pipeStatus?.current_stage && running && (
                <span className="small" style={{ marginLeft: 10 }}>stage: {humanIssue(pipeStatus.current_stage)}</span>
              )}
            </div>
            {!running && (
              <button className="btn" onClick={() => setPipeStatus(null)} style={{ padding: '3px 8px', fontSize: 11 }}>✕</button>
            )}
          </div>
          {(pipeStatus?.stages || []).map((s, i) => (
            <div className="pipeline-stage" key={i}>
              <div className={`stage-icon ${s.error ? '' : 'stage-done'}`}>{s.error ? '✕' : '✓'}</div>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, fontSize: 12 }}>{humanIssue(s.name)}</div>
                <div className="small">{s.detail || s.error}</div>
              </div>
              <span className="tag">{s.elapsed_s}s</span>
            </div>
          ))}
          {pipeStatus?.error && <p style={{ color: 'var(--danger)', marginTop: 8, fontSize: 12 }}>{pipeStatus.error}</p>}
        </div>
      )}

      {/* Stats */}
      <div className="grid stats fade-in">
        <div className="card card-glow">
          <div className="stat-label">Total cases</div>
          <div className="stat">{totalCases}</div>
          <div className="bar bar-brand" style={{ marginTop: 8 }}>
            <i style={{ width: '100%' }} />
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Open</div>
          <div className="stat">{core.open_cases || 0}</div>
          <div className="bar bar-brand" style={{ marginTop: 8 }}>
            <i style={{ width: totalCases ? `${((core.open_cases || 0) / totalCases) * 100}%` : '0%' }} />
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Critical + High</div>
          <div className="stat">{(core.critical_priority || 0) + (core.high_priority || 0)}</div>
          <div className="bar bar-danger" style={{ marginTop: 8 }}>
            <i style={{ width: totalCases ? `${(((core.critical_priority || 0) + (core.high_priority || 0)) / totalCases) * 100}%` : '0%' }} />
          </div>
        </div>
        <div className="card">
          <div className="stat-label">Financial exposure</div>
          <div className="stat">{formatMoney(core.total_exposure)}</div>
          <div className="stat-change">{totalCases} discrepancies tracked</div>
        </div>
      </div>

      {/* Issue + Decisions + Priority */}
      <div className="grid two" style={{ marginTop: 16 }}>
        <section className="card fade-in">
          <h2 className="section-title">Issue landscape</h2>
          <div className="list">
            {issues.map((x: any) => {
              const pct = totalCases ? (x.count / totalCases) * 100 : 0;
              return (
                <div className="row" key={x.issue_type}>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{humanIssue(x.issue_type)}</div>
                    <div className="bar bar-brand" style={{ width: '100%', marginTop: 3 }}>
                      <i style={{ width: `${pct}%` }} />
                    </div>
                  </div>
                  <b style={{ fontSize: 14 }}>{x.count}</b>
                </div>
              );
            })}
          </div>
        </section>

        <div className="grid" style={{ gap: 14 }}>
          <section className="card fade-in">
            <h2 className="section-title">Review decisions</h2>
            {decisions.length === 0 ? (
              <div className="empty-state" style={{ padding: 16 }}>
                <span className="muted" style={{ fontSize: 12 }}>No reviews yet</span>
              </div>
            ) : (
              <div className="list">
                {decisions.map((x: any) => (
                  <div className="row" key={x.decision}>
                    <span className={`badge ${x.decision?.toLowerCase()}`}>{x.decision}</span>
                    <b>{x.count}</b>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card fade-in">
            <h2 className="section-title">Priority distribution</h2>
            <div className="list">
              {[
                { label: 'Critical', count: core.critical_priority || 0, cls: 'critical' },
                { label: 'High', count: core.high_priority || 0, cls: 'high' },
                { label: 'Medium', count: core.medium_priority || 0, cls: 'medium' },
                { label: 'Low', count: core.low_priority || 0, cls: 'low' },
              ].map((p) => (
                <div className="row" key={p.label}>
                  <span className={`badge ${p.cls}`}>{p.label}</span>
                  <b>{p.count}</b>
                </div>
              ))}
            </div>
          </section>
        </div>
      </div>

      {/* Top Vendors */}
      {vendors.length > 0 && (
        <section className="card fade-in" style={{ marginTop: 16 }}>
          <h2 className="section-title">Top vendors by exposure</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Vendor</th>
                  <th>Cases</th>
                  <th>Total exposure</th>
                  <th style={{ width: '35%' }}>Share</th>
                </tr>
              </thead>
              <tbody>
                {vendors.slice(0, 8).map((v: any) => {
                  const maxExp = vendors[0]?.total_exposure || 1;
                  return (
                    <tr key={v.vendor_code}>
                      <td><b>{v.vendor_code}</b></td>
                      <td>{v.case_count}</td>
                      <td>{formatMoney(v.total_exposure)}</td>
                      <td>
                        <div className="bar bar-warn" style={{ width: '100%' }}>
                          <i style={{ width: `${(v.total_exposure / maxExp) * 100}%` }} />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* Feedback insights + evaluation metrics */}
      <div className="grid two" style={{ marginTop: 16 }}>
        <section className="card fade-in">
          <h2 className="section-title">Feedback insights</h2>
          {!fb || fb.reviewed_cases === 0 ? (
            <div className="empty-state" style={{ padding: 16 }}>
              <span className="muted" style={{ fontSize: 12 }}>
                No reviews yet — confirm/reject cases to teach the prioritizer.
              </span>
            </div>
          ) : (
            <div>
              <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 10 }}>
                <b style={{ fontSize: 18 }}>{fb.reviewed_cases}</b>
                <span className="small">reviewed case(s)</span>
                <span className="tag" style={{ marginLeft: 'auto' }}>
                  {fb.stage_b_active ? 'ML ranker active' : `ML ranker at ${fb.stage_b_min_labels} reviews`}
                </span>
              </div>
              {fb.by_issue && Object.keys(fb.by_issue).length > 0 && (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr><th>Issue type</th><th>Confirmed</th><th>Rejected</th></tr>
                    </thead>
                    <tbody>
                      {Object.entries(fb.by_issue).map(([issue, counts]) => (
                        <tr key={issue}>
                          <td>{humanIssue(issue)}</td>
                          <td>{counts.CONFIRM || 0}</td>
                          <td>{counts.REJECT || 0}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="small" style={{ marginTop: 8, lineHeight: 1.6 }}>
                Confirm/reject history shifts case ranking (capped, Bayesian-smoothed).
                Each case shows its ranking explanation. Feedback never auto-closes cases.
              </p>
            </div>
          )}
        </section>

        <section className="card fade-in">
          <h2 className="section-title">Evaluation metrics</h2>
          {!metrics || !metrics.available ? (
            <div className="empty-state" style={{ padding: 16 }}>
              <span className="muted" style={{ fontSize: 12 }}>
                {metrics?.reason || 'No evaluation labels available for the current dataset.'}
              </span>
            </div>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr><th>Issue</th><th>Precision</th><th>Recall</th><th>F1</th></tr>
                </thead>
                <tbody>
                  {metrics.metrics.map((m) => (
                    <tr key={m.discrepancy_type}>
                      <td>{humanIssue(m.discrepancy_type)}</td>
                      <td>{m.precision.toFixed(2)}</td>
                      <td>{m.recall.toFixed(2)}</td>
                      <td><b>{m.f1.toFixed(2)}</b></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      {/* Quick links */}
      <div className="grid three" style={{ marginTop: 16 }}>
        <Link href="/cases" className="card" style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 20, marginBottom: 6, opacity: 0.5 }}>🔍</div>
          <b style={{ fontSize: 13 }}>Investigation queue</b>
          <div className="small">Review prioritized discrepancies</div>
        </Link>
        <Link href="/patterns" className="card" style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 20, marginBottom: 6, opacity: 0.5 }}>🔗</div>
          <b style={{ fontSize: 13 }}>Pattern intelligence</b>
          <div className="small">Explore recurring error signals</div>
        </Link>
        <a href={api.exportCases('csv')} target="_blank" className="card" style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 20, marginBottom: 6, opacity: 0.5 }}>📊</div>
          <b style={{ fontSize: 13 }}>Export report</b>
          <div className="small">Download full investigation CSV</div>
        </a>
      </div>
    </main>
  );
}
