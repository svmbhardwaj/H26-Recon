"""ReconAI pipeline orchestrator.

Runs every processing stage in order, builds all outputs into a temporary
SQLite database and swaps it into place atomically on success, so a failed
run can never leave a half-written database. Stored human-review decisions
are carried across the swap and re-applied onto the rebuilt cases; decisions
whose case vanished are reported as orphans instead of being dropped.

Runs are serialised with a lock file (with stale-lock recovery) so two runs
cannot overlap. Every stage is individually timed and can fail independently;
the result object names the exact stage that failed.

Usage
-----
    python -m app.pipeline          # from backend/
    POST /api/pipeline/run          # from the API (background job)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

import pandas as pd

from . import config
from .reconciliation import reconcile, load_data
from .investigation import build_investigation_cases, save_cases_to_sqlite
from .anomaly import detect_anomalies
from .merge_intelligence import merge_anomaly_signals
from .pattern_intelligence import detect_patterns, enrich_cases_with_patterns, save_pattern_outputs
from .root_cause import enrich_root_cause, save_root_cause_outputs
from .rag import enrich_with_rag, save_outputs as save_rag_outputs, save_kb
from .human_review import init_review_tables, reapply_decisions

ROOT = config.ROOT
DATA = config.DATA_DIR
DB = config.DB_PATH

CORE_TABLES = (
    "investigation_cases", "investigation_cases_patterned",
    "investigation_cases_explained", "investigation_cases_rag",
    "error_patterns", "pattern_memberships", "vendors",
)

# Live run state for the API status endpoint (latest run in this process).
RUN_STATE: dict = {
    "state": "idle",          # idle | running | ok | failed | blocked
    "started_at": None,
    "finished_at": None,
    "current_stage": None,
    "stages": [],
    "error": "",
    "failed_stage": "",
    "ok": None,
    "carried_over_reviews": 0,
    "orphaned_reviews": 0,
    "pid": None,
}


@dataclass
class StageResult:
    name: str
    rows: int = 0
    elapsed_s: float = 0.0
    detail: str = ""
    error: str = ""


@dataclass
class PipelineResult:
    ok: bool = True
    stages: List[StageResult] = field(default_factory=list)
    total_elapsed_s: float = 0.0
    error: str = ""
    failed_stage: str = ""
    carried_over_reviews: int = 0
    orphaned_reviews: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ── File lock (serialises pipeline runs) ──────────────────────────────

class PipelineLocked(Exception):
    def __init__(self, holder_pid: int, age_s: float):
        self.holder_pid = holder_pid
        self.age_s = age_s
        super().__init__(f"another pipeline run is in progress (pid {holder_pid}, {age_s:.0f}s)")


def _read_lock(lock_path: Path) -> Optional[int]:
    try:
        return int(lock_path.read_text().strip())
    except Exception:
        return None


def acquire_lock(lock_path: Path | None = None) -> Path:
    """Acquire the pipeline lock, raising PipelineLocked if a live run holds it.

    A lock whose holder pid no longer exists (crashed run) is treated as
    stale and broken automatically.
    """
    lock_path = Path(lock_path) if lock_path else config.PIPELINE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = _read_lock(lock_path)
    if holder is not None:
        stale = not _pid_alive(holder) or _lock_age(lock_path) > config.LOCK_STALE_SECONDS
        if not stale:
            raise PipelineLocked(holder, _lock_age(lock_path))
        try:
            lock_path.unlink()
        except OSError:
            pass
    lock_path.write_text(str(os.getpid()))
    return lock_path


def release_lock(lock_path: Path | None = None) -> None:
    lock_path = Path(lock_path) if lock_path else config.PIPELINE_LOCK_PATH
    holder = _read_lock(lock_path)
    if holder == os.getpid():
        try:
            lock_path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _lock_age(lock_path: Path) -> float:
    try:
        return max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return 0.0


# ── Pipeline ──────────────────────────────────────────────────────────

def run_pipeline(data_dir: Path | None = None, db_path: Path | None = None,
                 lock: bool = True) -> PipelineResult:
    """Execute the full ReconAI processing pipeline end-to-end."""
    data_dir = Path(data_dir) if data_dir else config.DATA_DIR
    db_path = Path(db_path) if db_path else config.DB_PATH
    use_temp_db = db_path == config.DB_PATH

    result = PipelineResult()
    t0 = time.perf_counter()
    lock_path: Path | None = None

    RUN_STATE.update({
        "state": "running", "started_at": _utcnow(), "finished_at": None,
        "current_stage": None, "stages": [], "error": "", "failed_stage": "",
        "ok": None, "carried_over_reviews": 0, "orphaned_reviews": 0,
        "pid": os.getpid(),
    })

    def stage(name: str):
        """Context-helper returning (timer-start, StageResult) for a stage."""
        t = time.perf_counter()
        sr = StageResult(name=name)
        RUN_STATE["current_stage"] = name
        return t, sr

    def _finish_stage(sr: StageResult) -> None:
        result.stages.append(sr)
        RUN_STATE["stages"] = [asdict(s) for s in result.stages]
        if sr.error:
            result.ok, result.error, result.failed_stage = False, sr.error, sr.name

    try:
        if lock:
            try:
                lock_path = acquire_lock(config.PIPELINE_LOCK_PATH)
            except PipelineLocked as exc:
                result.ok = False
                result.error = str(exc)
                result.failed_stage = "lock"
                RUN_STATE.update({"state": "blocked", "error": str(exc),
                                  "finished_at": _utcnow()})
                return result

        tmp_db: Optional[Path] = None
        if use_temp_db:
            # Build into a temp DB and swap atomically on success.
            fd, tmp_name = tempfile.mkstemp(prefix="reconai_build_", suffix=".db", dir=str(data_dir))
            os.close(fd)
            tmp_db = Path(tmp_name)
            os.unlink(tmp_db)  # sqlite will create it; avoid empty-file confusion
            build_db = tmp_db
        else:
            build_db = db_path

        # ── Stage 1: Load source data ────────────────────────────────
        t, sr = stage("load_data")
        try:
            invoices, ledger, gst = load_data(data_dir)
            sr.rows = len(invoices)
            sr.detail = f"Loaded {len(invoices)} invoices, {len(ledger)} ledger, {len(gst)} GST rows"
        except Exception as exc:
            sr.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            sr.elapsed_s = round(time.perf_counter() - t, 3)
            result.stages.append(sr)
            if sr.error:
                result.ok, result.error, result.failed_stage = False, sr.error, sr.name

        if result.ok:
            # ── Stage 2: 3-way reconciliation ────────────────────────
            t, sr = stage("reconciliation")
            try:
                recon = reconcile(invoices, ledger, gst)
                recon.to_csv(data_dir / "reconciliation_results.csv", index=False)
                flagged = int((recon["issue_count"] > 0).sum())
                sr.rows = len(recon)
                sr.detail = f"Reconciled {len(recon)} rows; flagged {flagged}"
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        if result.ok:
            # ── Stage 3: Build investigation cases ───────────────────
            t, sr = stage("investigation_cases")
            try:
                cases = build_investigation_cases(recon, invoices)
                cases.to_csv(data_dir / "investigation_cases.csv", index=False)
                save_cases_to_sqlite(cases, build_db)
                sr.rows = len(cases)
                sr.detail = f"Created {len(cases)} investigation cases"
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        if result.ok:
            # ── Stage 4: ML anomaly detection ────────────────────────
            t, sr = stage("anomaly_detection")
            try:
                anomalies = detect_anomalies(invoices)
                anomalies.to_csv(data_dir / "anomaly_results.csv", index=False)
                ml_count = int(anomalies["ml_anomaly"].sum())
                sr.rows = len(anomalies)
                sr.detail = f"Scored {len(anomalies)} transactions; {ml_count} ML anomalies"
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        if result.ok:
            # ── Stage 5: Merge ML intelligence with cases ────────────
            t, sr = stage("merge_intelligence")
            try:
                enriched = merge_anomaly_signals(cases, anomalies)
                enriched.to_csv(data_dir / "investigation_cases_enriched.csv", index=False)
                sr.rows = len(enriched)
                sr.detail = f"Enriched {len(enriched)} cases with anomaly signals"
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        if result.ok:
            # ── Stage 6: Error pattern intelligence ──────────────────
            t, sr = stage("pattern_intelligence")
            try:
                patterns, memberships = detect_patterns(enriched, invoices)
                patterned = enrich_cases_with_patterns(enriched, patterns, memberships)
                save_pattern_outputs(patterns, memberships, patterned, data_dir, db_path=build_db)
                sr.rows = len(patterns)
                sr.detail = f"Detected {len(patterns)} patterns across {len(memberships)} memberships"
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        if result.ok:
            # ── Stage 7: Root-cause & exposure analysis ──────────────
            t, sr = stage("root_cause_analysis")
            try:
                explained = enrich_root_cause(patterned, data_dir)
                save_root_cause_outputs(explained, data_dir, db_path=build_db)
                sr.rows = len(explained)
                sr.detail = f"Root-cause explanations for {len(explained)} cases"
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        if result.ok:
            # ── Stage 8: GST RAG enrichment ──────────────────────────
            t, sr = stage("gst_rag_enrichment")
            try:
                save_kb()
                rag_df = enrich_with_rag(data_dir)
                save_rag_outputs(rag_df, data_dir, db_path=build_db)
                sr.rows = len(rag_df)
                sr.detail = f"RAG-enriched {len(rag_df)} cases with GST context"
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        if result.ok:
            # ── Stage 9: Review/audit tables + decision carry-over ───
            t, sr = stage("review_carryover")
            try:
                init_review_tables(build_db)
                # Reviews may reference the previous DB while we build the temp one.
                prior_db = db_path if use_temp_db and db_path.exists() else build_db
                if use_temp_db and prior_db != build_db:
                    _copy_review_history(prior_db, build_db)
                carry = reapply_decisions(build_db)
                result.carried_over_reviews = carry["applied"]
                result.orphaned_reviews = len(carry["orphans"])
                orphan_detail = f"; {len(carry['orphans'])} orphaned decision(s) retained" if carry["orphans"] else ""
                sr.detail = (f"Re-applied {carry['applied']} stored review decision(s)"
                             f"{orphan_detail}")
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        if result.ok:
            # ── Stage 10: Feedback-driven prioritization ─────────────
            t, sr = stage("feedback_ranking")
            try:
                from .feedback_model import refresh_ranking_in_db, train_ranker
                rank_meta = train_ranker(build_db, models_dir=config.MODELS_DIR)
                info = refresh_ranking_in_db(build_db)
                ml_note = " + stage-B ML ranker" if info.get("ml_active") else ""
                adjusted = info.get("feedback_adjusted_cases", 0)
                sr.detail = (f"Stage-A priors applied to {adjusted} case(s){ml_note}; "
                             f"ranked by base score + capped feedback adjustment"
                             + (f" (CV AUC {rank_meta['cv_metrics'].get('roc_auc')})" if rank_meta and rank_meta.get("cv_metrics", {}).get("roc_auc") else ""))
            except Exception as exc:
                sr.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                sr.elapsed_s = round(time.perf_counter() - t, 3)
                _finish_stage(sr)

        # ── Atomic swap ──────────────────────────────────────────────
        if result.ok and use_temp_db and tmp_db is not None:
            init_review_tables(build_db)
            _fsync_file(build_db)
            os.replace(build_db, db_path)
            tmp_db = None

    except Exception as exc:
        result.ok = False
        if not result.error:
            result.error = f"{type(exc).__name__}: {exc}"
        if not result.failed_stage:
            result.failed_stage = "unknown"
    finally:
        # Clean up temp build DB on failure.
        if use_temp_db and tmp_db is not None and tmp_db.exists():
            try:
                tmp_db.unlink()
            except OSError:
                pass
        if lock_path is not None:
            release_lock(lock_path)

    result.total_elapsed_s = round(time.perf_counter() - t0, 3)
    RUN_STATE.update({
        "state": "ok" if result.ok else "failed",
        "finished_at": _utcnow(),
        "ok": result.ok,
        "error": result.error,
        "failed_stage": result.failed_stage,
        "carried_over_reviews": result.carried_over_reviews,
        "orphaned_reviews": result.orphaned_reviews,
    })
    try:
        _record_run_history(result)
    except Exception:
        pass  # history recording must never break the pipeline result
    return result


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_review_history(source_db: Path, target_db: Path) -> None:
    """Copy review_decisions/audit_log from the live DB into the build DB."""
    init_review_tables(source_db)
    init_review_tables(target_db)
    src = sqlite3.connect(source_db)
    dst = sqlite3.connect(target_db)
    try:
        rows = src.execute(
            "SELECT case_id, decision, reviewer, notes, decided_at, previous_status FROM review_decisions"
        ).fetchall()
        dst.executemany(
            """INSERT INTO review_decisions(case_id, decision, reviewer, notes, decided_at, previous_status)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(case_id) DO UPDATE SET
                 decision=excluded.decision, reviewer=excluded.reviewer, notes=excluded.notes,
                 decided_at=excluded.decided_at, previous_status=excluded.previous_status""",
            rows,
        )
        audit = src.execute(
            "SELECT case_id, event_type, actor, event_data, created_at FROM audit_log"
        ).fetchall()
        dst.executemany(
            "INSERT INTO audit_log(case_id,event_type,actor,event_data,created_at) VALUES (?,?,?,?,?)",
            audit,
        )
        dst.commit()
    finally:
        src.close()
        dst.close()


def _record_run_history(result: PipelineResult) -> None:
    """Append a run record to data/pipeline_runs.json for the status endpoint."""
    import json
    from datetime import datetime, timezone

    history_path = config.DATA_DIR / "pipeline_runs.json"
    runs = []
    if history_path.exists():
        try:
            runs = json.loads(history_path.read_text())
        except Exception:
            runs = []
    runs.append({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "ok": result.ok,
        "total_elapsed_s": result.total_elapsed_s,
        "failed_stage": result.failed_stage,
        "error": result.error[:500],
        "carried_over_reviews": result.carried_over_reviews,
        "orphaned_reviews": result.orphaned_reviews,
        "stages": [asdict(s) for s in result.stages],
    })
    history_path.write_text(json.dumps(runs[-50:], indent=2))


if __name__ == "__main__":
    r = run_pipeline()
    for s in r.stages:
        marker = "ERR" if s.error else "ok "
        print(f"  [{marker}] {s.name}: {s.detail}  ({s.elapsed_s}s)")
    print(f"\nPipeline {'OK' if r.ok else 'FAILED'} in {r.total_elapsed_s}s"
          f" (carried {r.carried_over_reviews} review(s), {r.orphaned_reviews} orphaned)")
    if r.error:
        print(f"Error in stage '{r.failed_stage}': {r.error}")
