'use client';

import { useParams } from 'next/navigation';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import { api, formatMoney, formatPercent, humanIssue, priorityColor, statusColor } from '../../../lib/api';

export default function CasePage() {
  const { caseId } = useParams<{ caseId: string }>();
  const [c, setC] = useState<any>(null);
  const [reviews, setReviews] = useState<any[]>([]);
  const [audit, setAudit] = useState<any[]>([]);
  const [notes, setNotes] = useState('');
  const [reviewer, setReviewer] = useState('Analyst');
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState('');

  const load = () => {
    api.caseById(caseId).then((x) => {
      setC(x.case);
      setReviews(x.reviews || []);
      setAudit(x.audit || []);
    }).catch(() => {});
  };

  useEffect(() => {
    load();
  }, [caseId]);

  const submitReview = async (decision: string) => {
    setBusy(true);
    try {
      await api.review(caseId, decision, notes, reviewer);
      setToast(`Decision recorded: ${decision}`);
      setNotes('');
      load();
      setTimeout(() => setToast(''), 4000);
    } catch (e: any) {
      setToast(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  if (!c) {
    return (
      <main className="main">
        <div className="loading">Loading investigation</div>
      </main>
    );
  }

  const anomalyScore = Number(c.anomaly_score || 0);
  const anomalyLevel = anomalyScore >= 70 ? 'High' : anomalyScore >= 40 ? 'Moderate' : 'Low';
  const mlAnomaly = c.ml_anomaly === 1 || c.ml_anomaly === true;

  return (
    <main className="main">
      {/* Toast */}
      {toast && (
        <div className="card fade-in" style={{
          position: 'fixed', top: 68, right: 28, zIndex: 100,
          borderColor: 'var(--success-border)',
          padding: '10px 16px', fontSize: 13, fontWeight: 600, color: 'var(--success)',
        }}>
          {toast}
        </div>
      )}

      {/* Header */}
      <div className="page-head">
        <div>
          <div className="eyebrow">Investigation case</div>
          <h1 className="title">{c.case_id}</h1>
          <div className="subtitle" style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
            <span className={`badge ${priorityColor(c.priority_band)}`}>{c.priority_band}</span>
            <span className={`badge ${statusColor(c.status)}`}>{c.status}</span>
            <span className="muted">·</span>
            <span>{humanIssue(c.issue_type)}</span>
            <span className="muted">·</span>
            <span className="mono">{c.invoice_id}</span>
          </div>
        </div>
        <Link href="/cases" className="btn">← Back to queue</Link>
      </div>

      <div className="grid case-grid fade-in">
        {/* Left column */}
        <div className="grid" style={{ gap: 14 }}>
          {/* Root cause */}
          <section className="card">
            <h2 className="section-title">Root cause analysis</h2>
            <div className="evidence">
              <b>{c.root_cause || c.issue_label || 'Discrepancy detected'}</b>
              <p>{c.root_cause_detail || c.investigation_explanation || c.evidence || 'Review the linked financial records.'}</p>
            </div>

            <div className="grid stats" style={{ marginTop: 14, gridTemplateColumns: 'repeat(4, 1fr)' }}>
              <div>
                <div className="stat-label">Exposure</div>
                <b style={{ fontSize: 16 }}>{formatMoney(c.financial_exposure_refined || c.financial_exposure)}</b>
              </div>
              <div>
                <div className="stat-label">Confidence</div>
                <b style={{ fontSize: 16 }}>{Number(c.confidence_score ?? c.match_confidence ?? 0).toFixed(0)}/100</b>
              </div>
              <div>
                <div className="stat-label">ML anomaly</div>
                <b style={{ fontSize: 16 }}>{anomalyScore.toFixed(1)}</b>
                <div className="small">{anomalyLevel}</div>
              </div>
              <div>
                <div className="stat-label">Priority</div>
                <b style={{ fontSize: 16 }}>{Number(c.final_priority_score || c.priority_score || 0).toFixed(1)}</b>
              </div>
            </div>

            {/* Confidence breakdown */}
 {c.confidence_breakdown && (() => {
              try {
                const bd = JSON.parse(String(c.confidence_breakdown).replace(/'/g, '"')) as {
                  match_certainty: number; evidence_strength: number; issue_base_rate: number; formula: string;
                };
                return (
                  <div style={{ marginTop: 14, padding: '10px 14px', background: '#f7f8fa', borderRadius: 'var(--radius-sm)', border: '1px solid #e8eaee' }}>
                    <div className="stat-label" style={{ marginBottom: 6 }}>Confidence breakdown</div>
                    {[
                      { label: 'Match certainty', value: bd.match_certainty },
                      { label: 'Evidence strength', value: bd.evidence_strength },
                      { label: 'Issue base rate', value: bd.issue_base_rate },
                    ].map((row) => (
                      <div key={row.label} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                        <span className="small" style={{ width: 130 }}>{row.label}</span>
                        <div className="bar bar-brand" style={{ flex: 1 }}><i style={{ width: `${row.value}%` }} /></div>
                        <span className="small" style={{ width: 40, textAlign: 'right' }}>{row.value}</span>
                      </div>
                    ))}
                    <div className="small muted" style={{ marginTop: 4 }}>{bd.formula}</div>
                  </div>
                );
              } catch { return null; }
            })()}

            {/* Why ranked here */}
            {c.ranking_explanation && (
              <div style={{ marginTop: 10, padding: '10px 14px', background: '#f7f8fa', borderRadius: 'var(--radius-sm)', border: '1px solid #e8eaee' }}>
                <div className="stat-label" style={{ marginBottom: 3 }}>Why ranked here</div>
                <span style={{ fontSize: 13, color: 'var(--ink-secondary)' }}>{c.ranking_explanation}</span>
              </div>
            )}
          </section>

          {/* Evidence */}
          <section className="card">
            <h2 className="section-title">Evidence detail</h2>
            {String(c.evidence || c.evidence_detail || '').includes(' | ') || String(c.evidence_detail || '').includes(' | ') ? (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.9, color: 'var(--ink-secondary)' }}>
                {String(c.evidence_detail || c.evidence).split(' | ').map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            ) : (
              <p style={{ fontSize: 13, lineHeight: 1.8, color: 'var(--ink-secondary)' }}>
                {c.evidence_detail || c.evidence || 'No detailed evidence available.'}
              </p>
            )}
            {c.likely_reason && (
              <div style={{ marginTop: 12, padding: '10px 14px', background: '#f7f8fa', borderRadius: 'var(--radius-sm)', border: '1px solid #e8eaee' }}>
                <div className="stat-label" style={{ marginBottom: 3 }}>Likely reason / recommended action</div>
                <span style={{ fontSize: 13, color: 'var(--ink-secondary)' }}>{c.likely_reason}</span>
              </div>
            )}
            {c.recommended_action_refined && c.recommended_action_refined !== c.likely_reason && (
              <div style={{ marginTop: 8, padding: '10px 14px', background: '#f7f8fa', borderRadius: 'var(--radius-sm)', border: '1px solid #e8eaee' }}>
                <div className="stat-label" style={{ marginBottom: 3 }}>Recommended action</div>
                <span style={{ fontSize: 13, color: 'var(--ink-secondary)' }}>{c.recommended_action_refined}</span>
              </div>
            )}
          </section>

          {/* Financial impact */}
          {c.financial_impact_explanation && (
            <section className="card">
              <h2 className="section-title">Financial impact</h2>
              <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', marginBottom: 6 }}>
                <b style={{ fontSize: 22 }}>{formatMoney(c.financial_impact_amount)}</b>
                {c.financial_impact_type && <span className="tag">{c.financial_impact_type}</span>}
              </div>
              <p style={{ fontSize: 13, lineHeight: 1.8, color: 'var(--ink-secondary)' }}>{c.financial_impact_explanation}</p>
            </section>
          )}

          {/* ML anomaly */}
          {mlAnomaly && (
            <section className="card">
              <h2 className="section-title">ML anomaly signal</h2>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10 }}>
                <span className={`badge ${anomalyScore >= 70 ? 'high' : anomalyScore >= 40 ? 'medium' : 'low'}`}>
                  {anomalyLevel} ({anomalyScore.toFixed(1)}/100)
                </span>
                <span className="tag">{c.model || 'IsolationForest'}</span>
                <span className="tag">{c.investigation_signal}</span>
              </div>
              {c.anomaly_reason && (
                <p style={{ fontSize: 13, color: 'var(--ink-secondary)', lineHeight: 1.7 }}>
                  {c.anomaly_reason}
                </p>
              )}
            </section>
          )}

          {/* GST context */}
          <section className="card">
            <h2 className="section-title">GST rule context</h2>
            <div className="notice">
              Retrieved GST context is reference only. Final compliance decisions remain with the human reviewer.
            </div>
            {c.gst_rule_title && c.gst_rule_title !== 'No relevant rule retrieved' && (
              <div className="gst-card">
                <b>{c.gst_rule_title}</b>
                <p>{c.gst_rule_context}</p>
                {c.gst_rule_source && (
                  <div style={{ marginTop: 8, display: 'flex', gap: 6, alignItems: 'center' }}>
                    <span className="tag">{c.gst_rule_source}</span>
                    <span className="tag">Relevance: {Number(c.gst_rule_relevance || 0).toFixed(2)}</span>
                    {c.gst_rule_url && (
                      <a href={c.gst_rule_url} target="_blank" rel="noopener noreferrer" className="small" style={{ color: 'var(--brand)' }}>
                        Source →
                      </a>
                    )}
                  </div>
                )}
              </div>
            )}
          </section>

          {/* References */}
          <section className="card">
            <h2 className="section-title">Transaction references</h2>
            <div className="list">
              <div className="row"><span className="muted">Invoice ID</span><span className="mono">{c.invoice_id || '—'}</span></div>
              <div className="row"><span className="muted">Ledger Ref</span><span className="mono">{c.ledger_ref || '—'}</span></div>
              <div className="row"><span className="muted">GST Ref</span><span className="mono">{c.gst_ref || '—'}</span></div>
              <div className="row"><span className="muted">Vendor</span><b>{c.vendor_code || '—'}</b></div>
              <div className="row"><span className="muted">Category</span><span className="tag">{c.category || '—'}</span></div>
            </div>
          </section>
        </div>

        {/* Right column */}
        <aside className="grid" style={{ gap: 14, alignContent: 'start' }}>
          {/* Review panel */}
          <section className="card card-glow">
            <h2 className="section-title">Review &amp; act</h2>
            <div style={{ marginBottom: 10 }}>
              <label className="stat-label" style={{ display: 'block', marginBottom: 4 }}>Reviewer</label>
              <input className="input" style={{ width: '100%' }} value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Reviewer name" />
            </div>
            <div style={{ marginBottom: 10 }}>
              <label className="stat-label" style={{ display: 'block', marginBottom: 4 }}>Notes</label>
              <textarea className="input" style={{ width: '100%', minHeight: 90 }} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Explain your reasoning…" />
            </div>
            <div className="stat-label" style={{ marginBottom: 6 }}>Decision</div>
            <div className="btn-group">
              <button disabled={busy} className="btn success" onClick={() => submitReview('CONFIRM')}>✓ Confirm</button>
              <button disabled={busy} className="btn danger" onClick={() => submitReview('REJECT')}>✕ Reject</button>
              <button disabled={busy} className="btn warn" onClick={() => submitReview('NEEDS_REVIEW')}>⟳ Needs review</button>
            </div>
            {c.review_question && (
              <p className="small" style={{ marginTop: 8, lineHeight: 1.5 }}>💡 {c.review_question}</p>
            )}
          </section>

          {/* Patterns */}
          <section className="card">
            <h2 className="section-title">Related patterns</h2>
            {c.pattern_count && Number(c.pattern_count) > 0 ? (
              <div>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 8 }}>
                  <span className="badge medium">{c.pattern_signal}</span>
                  <span className="small">Confidence: {formatPercent(c.pattern_confidence)}</span>
                </div>
                <p style={{ fontSize: 13, color: 'var(--ink-secondary)', lineHeight: 1.7 }}>
                  {c.pattern_context || c.pattern_explanation}
                </p>
                {c.pattern_ids && (
                  <div style={{ marginTop: 8, display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                    {String(c.pattern_ids).split('|').map((pid: string) => (
                      <Link key={pid} href={`/patterns/${pid}`} className="tag" style={{ color: 'var(--brand)' }}>{pid} →</Link>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div className="empty-state" style={{ padding: 14 }}>
                <span className="muted" style={{ fontSize: 12 }}>No recurring pattern detected.</span>
              </div>
            )}
          </section>

          {/* Reviews */}
          {reviews.length > 0 && (
            <section className="card">
              <h2 className="section-title">Review history</h2>
              <div className="list">
                {reviews.map((r: any, i: number) => (
                  <div className="row" key={i} style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 3 }}>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <span className={`badge ${r.decision?.toLowerCase()}`}>{r.decision}</span>
                      <b style={{ fontSize: 12 }}>{r.reviewer}</b>
                    </div>
                    {r.notes && <p className="small" style={{ lineHeight: 1.5 }}>{r.notes}</p>}
                    <span className="small">{r.decided_at}</span>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* Audit */}
          <section className="card">
            <h2 className="section-title">Audit trail</h2>
            {audit.length === 0 ? (
              <div className="empty-state" style={{ padding: 14 }}>
                <span className="muted" style={{ fontSize: 12 }}>No audit events yet.</span>
              </div>
            ) : (
              <div className="list">
                {audit.map((a: any, i: number) => (
                  <div className="row" key={i} style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 2 }}>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <span className="tag">{a.event_type}</span>
                      <b style={{ fontSize: 11 }}>{a.actor}</b>
                    </div>
                    {a.event_data && <p className="small" style={{ lineHeight: 1.5 }}>{a.event_data}</p>}
                    <span className="small">{a.created_at}</span>
                  </div>
                ))}
              </div>
            )}
          </section>
        </aside>
      </div>
    </main>
  );
}
