'use client';

import { useState } from 'react';
import Link from 'next/link';
import { API_BASE, api, humanIssue, type PipelineStatus, type UploadPreview } from '../../lib/api';

const FORMATS = {
  invoices: {
    columns: ['invoice_id', 'vendor_code', 'invoice_date', 'taxable_amount', 'tax_rate', 'tax_amount', 'total_amount'],
    example: [
      ['INV-000001', 'V0009', '2026-07-31', '110280.73', '12.0', '13233.69', '123514.42'],
      ['INV-000002', 'V0042', '2026-07-10', '24450.16', '18.0', '4401.03', '28851.19'],
    ],
  },
  ledger: {
    columns: ['ledger_ref', 'invoice_id', 'vendor_code', 'entry_date', 'taxable_amount', 'tax_amount', 'total_amount'],
    example: [
      ['LED-000001', 'INV-000001', 'V0009', '2026-07-31', '110280.73', '13233.69', '123514.42'],
      ['LED-000002', 'INV-000002', 'V0042', '2026-07-10', '24450.16', '4401.03', '28851.19'],
    ],
  },
  gst: {
    columns: ['gst_ref', 'invoice_id', 'vendor_code', 'filing_date', 'taxable_amount', 'tax_rate', 'tax_amount'],
    example: [
      ['GST-000001', 'INV-000001', 'V0009', '2026-08-03', '110280.73', '12.0', '13233.69'],
      ['GST-000002', 'INV-000002', 'V0042', '2026-07-13', '24450.16', '18.0', '4401.03'],
    ],
  },
};

type FileKey = 'invoices' | 'ledger' | 'gst';
const FILE_KEYS: FileKey[] = ['invoices', 'ledger', 'gst'];
const EMPTY_FILES: Record<FileKey, File | null> = { invoices: null, ledger: null, gst: null };

function label(key: FileKey): string {
  return key === 'gst' ? 'GST Records' : key.charAt(0).toUpperCase() + key.slice(1);
}

export default function UploadPage() {
  const [files, setFiles] = useState<Record<FileKey, File | null>>({ ...EMPTY_FILES });
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<{ backup_dir: string | null; pipeline_started: boolean } | null>(null);
  const [pipeStatus, setPipeStatus] = useState<PipelineStatus | null>(null);
  const [error, setError] = useState('');
  const [activeTab, setActiveTab] = useState<FileKey>('invoices');
  const [dragOver, setDragOver] = useState<FileKey | null>(null);

  const allSelected = FILE_KEYS.every((k) => files[k]);
  const previewOk = preview?.ok === true;

  const reset = () => {
    setFiles({ ...EMPTY_FILES });
    setPreview(null);
    setUploadResult(null);
    setPipeStatus(null);
    setError('');
  };

  const handleFile = (key: FileKey, file: File | null) => {
    setFiles((prev) => ({ ...prev, [key]: file }));
    setError('');
    setPreview(null);
    setUploadResult(null);
  };

  const handleDrop = (key: FileKey, e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(null);
    const file = e.dataTransfer.files?.[0];
    if (file && file.name.toLowerCase().endsWith('.csv')) {
      handleFile(key, file);
    } else if (file) {
      setError(`${file.name} is not a CSV file`);
    }
  };

  const runPreview = async () => {
    if (!allSelected) return;
    setPreviewing(true);
    setError('');
    setUploadResult(null);
    try {
      const result = await api.uploadPreview({ invoices: files.invoices!, ledger: files.ledger!, gst: files.gst! });
      setPreview(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Preview failed');
    } finally {
      setPreviewing(false);
    }
  };

  const pollStatus = (): Promise<PipelineStatus> =>
    new Promise((resolve) => {
      const tick = () => {
        api.pipelineStatus().then((s) => {
          setPipeStatus(s);
          if (s.state === 'running') {
            setTimeout(tick, 1500);
          } else {
            resolve(s);
          }
        }).catch(() => setTimeout(tick, 3000));
      };
      tick();
    });

  const commitUpload = async () => {
    if (!previewOk) return;
    setUploading(true);
    setError('');
    try {
      const result = await api.upload({ invoices: files.invoices!, ledger: files.ledger!, gst: files.gst! });
      setUploadResult({ backup_dir: result.backup_dir, pipeline_started: result.pipeline_started });
      if (result.pipeline_started) {
        await pollStatus();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const fmt = FORMATS[activeTab];
  const stages = pipeStatus?.stages || [];

  return (
    <main className="main">
      <div className="page-head">
        <div>
          <div className="eyebrow">Data integration</div>
          <h1 className="title">Upload your data</h1>
          <div className="subtitle">
            CSV files with any reasonable column names — the ingestion layer maps aliases
            (Invoice No, GSTIN of Supplier, Taxable Value, …), normalises dates and ₹ amounts,
            and shows you a validation preview before anything is written.
          </div>
        </div>
        <div className="btn-group">
          <button className="btn" onClick={reset} disabled={uploading}>Reset</button>
        </div>
      </div>

      <div className="grid two fade-in">
        {/* Left — dropzones */}
        <div className="grid" style={{ gap: 14 }}>
          {FILE_KEYS.map((key) => {
            const file = files[key];
            const src = preview?.sources?.[key === 'gst' ? 'gst_records' : key];
            return (
              <section className="card" key={key}>
                <h2 className="section-title">{label(key)} CSV</h2>
                <div
                  role="button"
                  tabIndex={0}
                  aria-label={`Drop or choose ${label(key)} CSV`}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); document.getElementById(`file-${key}`)?.click(); } }}
                  onDragOver={(e) => { e.preventDefault(); setDragOver(key); }}
                  onDragLeave={() => setDragOver(null)}
                  onDrop={(e) => handleDrop(key, e)}
                  style={{
                    border: `2px dashed ${dragOver === key ? 'var(--brand)' : '#d8dce3'}`,
                    borderRadius: 'var(--radius-sm)',
                    padding: '18px 14px',
                    textAlign: 'center',
                    cursor: 'pointer',
                    background: dragOver === key ? 'rgba(37,99,235,0.04)' : 'transparent',
                  }}
                  onClick={() => document.getElementById(`file-${key}`)?.click()}
                >
                  <input
                    id={`file-${key}`}
                    type="file"
                    accept=".csv"
                    onChange={(e) => handleFile(key, e.target.files?.[0] || null)}
                    style={{ display: 'none' }}
                  />
                  {file ? (
                    <span style={{ fontSize: 13 }}>
                      <b>{file.name}</b>
                      <span className="muted" style={{ marginLeft: 8 }}>({(file.size / 1024).toFixed(0)} KB)</span>
                    </span>
                  ) : (
                    <span className="muted" style={{ fontSize: 13 }}>Drag &amp; drop a CSV here, or click to choose</span>
                  )}
                </div>

                {src && (
                  <div className="small" style={{ marginTop: 8, lineHeight: 1.7 }}>
                    <span className="badge confirmed">{src.rows_out} / {src.rows_in} rows valid</span>
                    {Object.keys(src.columns_mapped || {}).length > 0 && (
                      <div style={{ marginTop: 6 }}>
                        <b>Mapped columns:</b>{' '}
                        {Object.entries(src.columns_mapped).map(([canon, orig]) => (
                          <code key={canon} className="tag" style={{ marginRight: 4, marginBottom: 2 }}>
                            {orig} → {canon}
                          </code>
                        ))}
                      </div>
                    )}
                    {src.columns_unmapped.length > 0 && (
                      <div style={{ marginTop: 4 }}>
                        <b>Ignored:</b> {src.columns_unmapped.join(', ')}
                      </div>
                    )}
                    {src.rejected_count > 0 && (
                      <div style={{ marginTop: 4, color: 'var(--danger)' }}>
                        <b>{src.rejected_count} row(s) rejected</b>
                        {src.rejected_sample.slice(0, 3).map((r, i) => (
                          <div key={i}>row {r.row}: {r.reason}</div>
                        ))}
                      </div>
                    )}
                    {src.warnings.map((w, i) => (
                      <div key={i} style={{ marginTop: 4, color: '#92600a' }}>⚠ {w}</div>
                    ))}
                    {src.missing_required_columns.length > 0 && (
                      <div style={{ marginTop: 4, color: 'var(--danger)' }}>
                        <b>Missing required columns:</b> {src.missing_required_columns.join(', ')}
                      </div>
                    )}
                  </div>
                )}
              </section>
            );
          })}

          <div className="btn-group">
            <button
              className={`btn ${allSelected && !previewOk ? 'primary' : ''}`}
              disabled={!allSelected || previewing || uploading}
              onClick={runPreview}
              style={{ padding: '12px 24px', fontSize: 14 }}
            >
              {previewing ? '⟳ Validating…' : '1 · Validate & preview'}
            </button>
            <button
              className={`btn ${previewOk ? 'primary' : ''}`}
              disabled={!previewOk || uploading}
              onClick={commitUpload}
              style={{ padding: '12px 24px', fontSize: 14 }}
            >
              {uploading ? '⟳ Uploading & running pipeline…' : '2 · Upload & run pipeline'}
            </button>
          </div>

          {preview && !preview.ok && (
            <div className="card" style={{ borderColor: 'var(--danger-border)' }}>
              <b style={{ color: 'var(--danger)', fontSize: 13 }}>Validation failed — live data untouched</b>
              <ul className="small" style={{ margin: '6px 0 0 16px', lineHeight: 1.8 }}>
                {preview.errors.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </div>
          )}

          {error && (
            <div className="card" style={{ borderColor: 'var(--danger-border)' }}>
              <b style={{ color: 'var(--danger)', fontSize: 13 }}>Error</b>
              <p className="small" style={{ marginTop: 4, lineHeight: 1.6, wordBreak: 'break-word' }}>{error}</p>
            </div>
          )}
        </div>

        {/* Right — format reference */}
        <div className="grid" style={{ gap: 14, alignContent: 'start' }}>
          <section className="card">
            <h2 className="section-title">Canonical format (aliases accepted)</h2>
            <div style={{ display: 'flex', gap: 4, marginBottom: 14 }}>
              {FILE_KEYS.map((key) => (
                <button
                  key={key}
                  className={`btn ${activeTab === key ? 'primary' : ''}`}
                  onClick={() => setActiveTab(key)}
                  style={{ fontSize: 12, padding: '5px 10px' }}
                >
                  {label(key)}
                </button>
              ))}
            </div>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    {fmt.columns.map((c) => <th key={c}>{c}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {fmt.example.map((row, i) => (
                    <tr key={i}>
                      {row.map((val, j) => <td key={j} className="mono">{val}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="small" style={{ marginTop: 10, lineHeight: 1.7 }}>
              <b>Notes:</b>
              <ul style={{ margin: '4px 0 0 16px' }}>
                <li>UTF-8 CSV with a header row</li>
                <li>Dates: YYYY-MM-DD preferred; DD/MM/YYYY handled (₹/commas stripped)</li>
                <li>Aliases like <code>Invoice No</code>, <code>Taxable Value</code>, <code>GST Rate</code> map automatically</li>
                <li>invoice_id is the join key; vendor_code groups by supplier</li>
                <li>Previous files are backed up (timestamped) before overwrite</li>
              </ul>
            </div>
          </section>

          <section className="card">
            <h2 className="section-title">How it works</h2>
            <div className="list">
              {[
                'Drop your three CSV files (column names can differ)',
                'Validate & preview: see mapping, row counts, rejected rows — nothing is written yet',
                'Upload & run: files are backed up, replaced, and the pipeline runs with live progress',
                'Investigation cases appear in the queue, ready for review',
              ].map((text, i) => (
                <div className="row" key={i} style={{ gap: 10 }}>
                  <span className="tag" style={{ minWidth: 20, textAlign: 'center' }}>{i + 1}</span>
                  <span style={{ fontSize: 13 }}>{text}</span>
                </div>
              ))}
            </div>
          </section>
        </div>
      </div>

      {/* Live pipeline progress */}
      {(uploading || pipeStatus) && (
        <div className="card fade-in" style={{ marginTop: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <b style={{ fontSize: 14 }}>
              Pipeline {pipeStatus?.state === 'running' ? 'running' : pipeStatus?.state === 'ok' ? 'completed' : pipeStatus?.state === 'failed' ? 'failed' : 'starting…'}
            </b>
            {uploadResult?.backup_dir && (
              <span className="tag" title={uploadResult.backup_dir}>previous files backed up</span>
            )}
          </div>

          {stages.length === 0 ? (
            <div className="pipeline-stage">
              <div className="stage-icon stage-done">⟳</div>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, fontSize: 12 }}>Starting pipeline…</div>
              </div>
            </div>
          ) : (
            stages.map((s, i) => (
              <div className="pipeline-stage" key={i}>
                <div className={`stage-icon ${s.error ? '' : 'stage-done'}`}>{s.error ? '✕' : '✓'}</div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600, fontSize: 12 }}>{humanIssue(s.name)}</div>
                  <div className="small">{s.detail || s.error}</div>
                </div>
                <span className="tag">{s.elapsed_s}s</span>
              </div>
            ))
          )}

          {pipeStatus?.state === 'ok' && (
            <div style={{ marginTop: 14, display: 'flex', gap: 8 }}>
              <Link href="/cases" className="btn primary">View investigation queue →</Link>
              <Link href="/" className="btn">Go to dashboard</Link>
            </div>
          )}
          {pipeStatus?.state === 'failed' && (
            <p style={{ color: 'var(--danger)', marginTop: 8, fontSize: 13 }}>
              Failed at stage <b>{pipeStatus.failed_stage}</b>: {pipeStatus.error}
            </p>
          )}
        </div>
      )}
    </main>
  );
}
