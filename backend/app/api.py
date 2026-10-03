from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import traceback
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import config
from .human_review import record_decision, init_review_tables, reapply_decisions
from .pipeline import RUN_STATE, PipelineLocked


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Bootstrap: ensure tables exist; run the pipeline only when truly fresh."""
    config.reconfigure()
    config.ensure_dirs()
    DB = config.DB_PATH
    try:
        init_review_tables(DB)
        with sqlite3.connect(DB) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='investigation_cases_rag'")
            has_cases = cur.fetchone() is not None
            row_count = 0
            if has_cases:
                row_count = conn.execute("SELECT COUNT(*) FROM investigation_cases_rag").fetchone()[0]
        if not has_cases or row_count == 0:
            print("[ReconAI Startup] Fresh deployment detected. Initializing database and models...")
            from .pipeline import run_pipeline
            res = run_pipeline()
            print(f"[ReconAI Startup] Initial pipeline finished: ok={res.ok}")
    except Exception as e:
        print(f"[ReconAI Startup] Note during initialization: {e}")
    yield


app = FastAPI(
    title="ReconAI API",
    version="1.1.0",
    description="API for ReconAI's reconciliation, investigation, pattern intelligence and human review workflow.",
    lifespan=lifespan,
)

# Configurable CORS for production deployment
import os

cors_origins_env = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:80,http://localhost,http://127.0.0.1"
)
if cors_origins_env.strip() == "*":
    allow_origins = ["*"]
    allow_credentials = False
else:
    allow_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Background pipeline task ─────────────────────────────────────────

_pipeline_task: Optional[asyncio.Task] = None


def _run_pipeline_blocking(data_dir: Path) -> dict:
    from .pipeline import run_pipeline
    result = run_pipeline(data_dir)
    payload = result.to_dict()
    return payload


async def _pipeline_job(data_dir: Path) -> None:
    global _pipeline_task
    try:
        await asyncio.to_thread(_run_pipeline_blocking, data_dir)
    except PipelineLocked as exc:
        RUN_STATE.update({"state": "blocked", "error": str(exc), "finished_at": None})
    except Exception as exc:
        RUN_STATE.update({
            "state": "failed", "error": f"{type(exc).__name__}: {exc}",
            "failed_stage": RUN_STATE.get("failed_stage") or "unknown",
            "finished_at": None,
        })
        traceback.print_exc()


class ReviewRequest(BaseModel):
    decision: str = Field(pattern=r"^(CONFIRM|REJECT|NEEDS_REVIEW)$")
    reviewer: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=2000)


def _connect() -> sqlite3.Connection:
    DB = config.DB_PATH
    init_review_tables(DB)
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(sql: str, params: tuple = ()) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


# ── Health ────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "ReconAI API", "version": app.version}


# ── Dashboard ─────────────────────────────────────────────────────────

@app.get("/api/dashboard")
def dashboard() -> dict:
    with _connect() as conn:
        cases = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_cases,
                 SUM(CASE WHEN status='CONFIRMED' THEN 1 ELSE 0 END) AS confirmed_cases,
                 SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END) AS rejected_cases,
                 SUM(CASE WHEN status='NEEDS_REVIEW' THEN 1 ELSE 0 END) AS needs_review,
                 COALESCE(SUM(financial_exposure), 0) AS total_exposure,
                 COALESCE(SUM(CASE WHEN priority_band='CRITICAL' THEN 1 ELSE 0 END), 0) AS critical_priority,
                 COALESCE(SUM(CASE WHEN priority_band='HIGH' THEN 1 ELSE 0 END), 0) AS high_priority,
                 COALESCE(SUM(CASE WHEN priority_band='MEDIUM' THEN 1 ELSE 0 END), 0) AS medium_priority,
                 COALESCE(SUM(CASE WHEN priority_band='LOW' THEN 1 ELSE 0 END), 0) AS low_priority
               FROM investigation_cases_rag"""
        ).fetchone()
        types = conn.execute(
            "SELECT issue_type, COUNT(*) AS count FROM investigation_cases_rag GROUP BY issue_type ORDER BY count DESC"
        ).fetchall()
        decisions = conn.execute(
            "SELECT decision, COUNT(*) AS count FROM review_decisions GROUP BY decision"
        ).fetchall()
        # Top vendors by exposure
        vendors = conn.execute(
            """SELECT vendor_code, COUNT(*) AS case_count,
                      COALESCE(SUM(financial_exposure), 0) AS total_exposure
               FROM investigation_cases_rag
               GROUP BY vendor_code ORDER BY total_exposure DESC LIMIT 10"""
        ).fetchall()
        # Severity distribution
        severity = conn.execute(
            "SELECT severity, COUNT(*) AS count FROM investigation_cases_rag GROUP BY severity ORDER BY count DESC"
        ).fetchall()
    return {
        "cases": dict(cases) if cases else {},
        "issue_breakdown": [dict(r) for r in types],
        "review_breakdown": [dict(r) for r in decisions],
        "top_vendors": [dict(r) for r in vendors],
        "severity_breakdown": [dict(r) for r in severity],
    }


# ── Cases ─────────────────────────────────────────────────────────────

@app.get("/api/cases")
def list_cases(
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    issue_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status.upper())
    if priority:
        clauses.append("priority_band = ?")
        params.append(priority.upper())
    if issue_type:
        clauses.append("issue_type = ?")
        params.append(issue_type)
    if search:
        clauses.append("(case_id LIKE ? OR invoice_id LIKE ? OR vendor_code LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    count = _one(f"SELECT COUNT(*) AS total FROM investigation_cases_rag{where}", tuple(params))
    rows = _rows(
        f"SELECT * FROM investigation_cases_rag{where} ORDER BY priority_score DESC, financial_exposure DESC LIMIT ? OFFSET ?",
        tuple(params + [limit, offset]),
    )
    return {"total": count["total"] if count else 0, "limit": limit, "offset": offset, "items": rows}


@app.get("/api/cases/{case_id}")
def get_case(case_id: str) -> dict:
    case = _one("SELECT * FROM investigation_cases_rag WHERE case_id = ?", (case_id,))
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    reviews = _rows("SELECT * FROM review_decisions WHERE case_id = ?", (case_id,))
    audit = _rows("SELECT * FROM audit_log WHERE case_id = ? ORDER BY created_at DESC", (case_id,))
    return {"case": case, "reviews": reviews, "audit": audit}


@app.post("/api/cases/{case_id}/review")
def review_case(case_id: str, payload: ReviewRequest) -> dict:
    try:
        return record_decision(case_id, payload.decision, payload.reviewer, payload.notes, config.DB_PATH)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ── Audit trail ───────────────────────────────────────────────────────

@app.get("/api/audit/{case_id}")
def case_audit(case_id: str) -> dict:
    case = _one("SELECT case_id FROM investigation_cases_rag WHERE case_id = ?", (case_id,))
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return {"case_id": case_id, "events": _rows("SELECT * FROM audit_log WHERE case_id = ? ORDER BY created_at ASC", (case_id,))}


# ── Patterns ──────────────────────────────────────────────────────────

@app.get("/api/patterns")
def list_patterns(limit: int = Query(100, ge=1, le=500)) -> dict:
    patterns = _rows("SELECT * FROM error_patterns ORDER BY financial_exposure DESC, pattern_confidence DESC LIMIT ?", (limit,))
    return {"items": patterns}


@app.get("/api/patterns/{pattern_id}")
def get_pattern(pattern_id: str) -> dict:
    pattern = _one("SELECT * FROM error_patterns WHERE pattern_id = ?", (pattern_id,))
    if not pattern:
        raise HTTPException(status_code=404, detail="Pattern not found")
    memberships = _rows("SELECT * FROM pattern_memberships WHERE pattern_id = ?", (pattern_id,))
    # Also return the actual case details for each membership
    case_ids = [m["case_id"] for m in memberships]
    cases = []
    if case_ids:
        placeholders = ",".join("?" for _ in case_ids)
        cases = _rows(
            f"SELECT case_id, invoice_id, issue_type, severity, financial_exposure, priority_band, status FROM investigation_cases_rag WHERE case_id IN ({placeholders})",
            tuple(case_ids),
        )
    return {"pattern": pattern, "memberships": memberships, "cases": cases}


# ── Vendors ───────────────────────────────────────────────────────────

@app.get("/api/vendors")
def list_vendors(limit: int = Query(50, ge=1, le=200)) -> dict:
    vendors = _rows(
        """SELECT v.*,
                  (SELECT COUNT(*) FROM investigation_cases_rag c WHERE c.vendor_code = v.vendor_code) AS case_count,
                  (SELECT COALESCE(SUM(financial_exposure), 0) FROM investigation_cases_rag c WHERE c.vendor_code = v.vendor_code) AS total_exposure
           FROM vendors v ORDER BY total_exposure DESC LIMIT ?""",
        (limit,),
    )
    return {"items": vendors}


@app.get("/api/vendors/{vendor_code}")
def vendor_profile(vendor_code: str) -> dict:
    vendor = _one("SELECT * FROM vendors WHERE vendor_code = ?", (vendor_code,))
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    cases = _rows(
        "SELECT * FROM investigation_cases_rag WHERE vendor_code = ? ORDER BY priority_score DESC",
        (vendor_code,),
    )
    return {"vendor": vendor, "case_count": len(cases), "cases": cases}


# ── GST Rule search ──────────────────────────────────────────────────

@app.get("/api/rules/search")
def search_rules(q: str = Query(..., min_length=2), limit: int = Query(5, ge=1, le=20)) -> dict:
    # Lightweight lexical search over the traceable local knowledge base.
    kb_path = config.KB_PATH
    if not kb_path.exists():
        return {"query": q, "items": []}
    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    terms = [t.lower() for t in q.split() if t.strip()]
    scored = []
    for item in kb:
        hay = " ".join(str(v) for v in item.values()).lower()
        score = sum(hay.count(term) for term in terms)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return {"query": q, "items": [{**item, "lexical_score": score} for score, item in scored[:limit]]}


# ── Pipeline orchestration ────────────────────────────────────────────

@app.post("/api/pipeline/run")
async def run_pipeline_endpoint() -> dict:
    """Start the full pipeline as a background job; poll /api/pipeline/status."""
    global _pipeline_task
    if RUN_STATE.get("state") == "running" and _pipeline_task and not _pipeline_task.done():
        raise HTTPException(status_code=409, detail="Pipeline is already running")
    from .pipeline import acquire_lock, PipelineLocked as PL, release_lock
    from . import config as _cfg
    lock_path = _cfg.PIPELINE_LOCK_PATH
    try:
        acquire_lock(lock_path)
        release_lock(lock_path)
    except PL as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _pipeline_task = asyncio.create_task(_pipeline_job(config.DATA_DIR))
    return {"started": True, "state": RUN_STATE.get("state"), "status_url": "/api/pipeline/status"}


@app.get("/api/pipeline/status")
def pipeline_status() -> dict:
    """Live pipeline state: idle/running/ok/failed plus per-stage progress."""
    populated = False
    total_cases = 0
    try:
        total = _one("SELECT COUNT(*) AS total FROM investigation_cases_rag")
        populated = bool(total and total["total"] > 0)
        total_cases = total["total"] if total else 0
    except Exception:
        pass
    state = dict(RUN_STATE)
    state["populated"] = populated
    state["total_cases"] = total_cases
    state["task_done"] = bool(_pipeline_task.done()) if _pipeline_task else None
    return state


@app.get("/api/pipeline/runs")
def pipeline_runs() -> dict:
    """Recent run history (last 50 runs, newest last)."""
    history_path = config.DATA_DIR / "pipeline_runs.json"
    if not history_path.exists():
        return {"runs": []}
    try:
        return {"runs": json.loads(history_path.read_text())}
    except Exception:
        return {"runs": []}


# ── Export ────────────────────────────────────────────────────────────

@app.get("/api/export/cases")
def export_cases(format: str = Query("csv", pattern=r"^(csv|json)$")) -> StreamingResponse:
    """Export all investigation cases as CSV or JSON."""
    rows = _rows("SELECT * FROM investigation_cases_rag ORDER BY priority_score DESC, financial_exposure DESC")
    if format == "json":
        content = json.dumps(rows, indent=2, ensure_ascii=False)
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=reconai_cases.json"},
        )
    # CSV
    if not rows:
        return StreamingResponse(io.BytesIO(b""), media_type="text/csv")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reconai_cases.csv"},
    )


@app.get("/api/export/audit")
def export_audit() -> StreamingResponse:
    """Export the full audit trail as CSV."""
    rows = _rows("SELECT * FROM audit_log ORDER BY created_at DESC")
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=reconai_audit.csv"},
    )


# ── Metrics ───────────────────────────────────────────────────────────

@app.get("/api/metrics")
def metrics(refresh: bool = Query(False, description="Re-run evaluation before returning")) -> dict:
    """Fresh evaluation metrics from the current dataset (never a stale CSV)."""
    from .evaluate import evaluate, has_ground_truth
    if not has_ground_truth(config.DATA_DIR):
        return {"available": False, "reason": "no ground_truth.csv for the current dataset; run the demo generator or upload labels", "metrics": []}
    try:
        df = evaluate(data_dir=config.DATA_DIR, refresh=refresh)
        return {"available": True, "metrics": df.to_dict(orient="records")}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Evaluation failed: {exc}") from exc


# ── Data upload ───────────────────────────────────────────────────────

@app.post("/api/upload/preview")
async def upload_preview(invoices: UploadFile = File(...), ledger: UploadFile = File(...), gst: UploadFile = File(...)) -> dict:
    """Validate and map uploaded CSVs without touching live data."""
    from .ingest import ingest_bundle
    payloads = {"invoices": await invoices.read(), "ledger": await ledger.read(), "gst_records": await gst.read()}
    bundle = ingest_bundle(payloads)
    preview = bundle.preview()
    return {"ok": preview["ok"], **preview}


@app.post("/api/upload")
async def upload_data(
    invoices: UploadFile = File(...),
    ledger: UploadFile = File(...),
    gst: UploadFile = File(...),
    vendors: Optional[UploadFile] = File(None),
    run: bool = Query(True, description="Run the pipeline after a successful upload"),
) -> dict:
    """Upload user CSV files, validate + map them, back up old files, then run the pipeline in the background.

    Live data is never overwritten unless every file validates. On validation
    failure the response lists per-source errors/rejections and nothing changes.
    """
    from .ingest import ingest_bundle, write_bundle
    payloads = {"invoices": await invoices.read(), "ledger": await ledger.read(), "gst_records": await gst.read()}
    if vendors is not None and vendors.filename:
        payloads["vendors"] = await vendors.read()

    bundle = ingest_bundle(payloads)
    preview = bundle.preview()
    if not bundle.ok:
        raise HTTPException(status_code=422, detail={"message": "Upload rejected; live data untouched", **preview})

    try:
        backup_dir = write_bundle(bundle, config.DATA_DIR, backup=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write uploaded data: {exc}") from exc

    pipeline_started = False
    if run:
        global _pipeline_task
        if not (RUN_STATE.get("state") == "running" and _pipeline_task and not _pipeline_task.done()):
            _pipeline_task = asyncio.create_task(_pipeline_job(config.DATA_DIR))
            pipeline_started = True

    return {
        "uploaded": {name: {"rows": src.rows_out, "columns_mapped": src.mapping, "rejected": len(src.rejected)} for name, src in bundle.sources.items()},
        "preview": preview,
        "backup_dir": str(backup_dir) if backup_dir else None,
        "pipeline_started": pipeline_started,
        "status_url": "/api/pipeline/status",
    }


@app.post("/api/score")
async def score_new_data(invoices: UploadFile = File(...)) -> dict:
    """Score new invoices against the saved anomaly model (no retraining)."""
    import pandas as pd
    from .anomaly import score as anomaly_score, load_model

    content = await invoices.read()
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse CSV: {exc}") from exc

    loaded = load_model()
    if loaded is None:
        raise HTTPException(status_code=400, detail="No trained model found. Run the pipeline first to train the anomaly model.")

    try:
        results = anomaly_score(df)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Scoring failed: {exc}") from exc

    anomalies = results[results["ml_anomaly"] == True]
    return {
        "total_scored": len(results),
        "anomalies_found": len(anomalies),
        "results": results.to_dict(orient="records"),
    }


# ── Feedback ─────────────────────────────────────────────────────────

@app.get("/api/feedback")
def feedback_panel() -> dict:
    """Feedback insights: review counts, confirm/reject rates, stage-B state."""
    from .feedback_model import feedback_summary
    return feedback_summary(config.DB_PATH)


@app.post("/api/feedback/rerank")
def feedback_rerank() -> dict:
    """Recompute feedback-adjusted ranking now (also runs at pipeline end)."""
    from .feedback_model import refresh_ranking_in_db
    return refresh_ranking_in_db(config.DB_PATH)


# ── Model info ────────────────────────────────────────────────────────

@app.get("/api/models")
def model_info() -> dict:
    """Show what trained model artifacts exist."""
    artifacts = []
    if config.MODELS_DIR.exists():
        for f in sorted(config.MODELS_DIR.iterdir()):
            if f.suffix == ".joblib":
                stat = f.stat()
                artifacts.append({
                    "name": f.name,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": stat.st_mtime,
                })
    return {
        "model_dir": str(config.MODELS_DIR),
        "artifacts": artifacts,
        "has_anomaly_model": (config.MODELS_DIR / "anomaly_model.joblib").exists(),
        "has_rag_retriever": (config.MODELS_DIR / "rag_vectorizer.joblib").exists(),
    }
