"""Feedback-driven prioritization.

Two stages, both ranking-only (feedback never auto-closes or auto-confirms a case):

Stage A (always on, works from the first review)
    Bayesian-smoothed confirm-rate priors by issue_type, vendor_code and
    pattern_id, blended into the case priority with a visible, capped weight.
    Smoothed rate = (confirmations + alpha * global_rate) / (n + alpha) with
    alpha = 5, so a handful of reviews cannot swing ranking wildly.

Stage B (activates after ``ML_MIN_LABELS`` labelled decisions with both
classes present)
    Trains a small xgboost classifier to predict P(confirmed) from case
    features; NEEDS_REVIEW decisions are treated as unlabeled and excluded.
    Cross-validated metrics are logged with the model artifact.

Every case gets a ``ranking_explanation`` showing the base score, the ML
boost and the feedback adjustment separately.
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from . import config

DB = config.DB_PATH

ALPHA = 5.0                 # Bayesian smoothing strength (pseudo-count)
FEEDBACK_WEIGHT = 0.10      # max share of final score driven by feedback prior
FEEDBACK_CAP = 8.0          # absolute cap on feedback adjustment (points)
ML_WEIGHT = 0.12            # max share of final score driven by the ML classifier
ML_CAP = 10.0               # absolute cap on ML boost (points)
ML_MIN_LABELS = 50          # stage-B activation threshold (both classes present)
ML_MODEL_PATH = "feedback_ranker.joblib"
ML_META_PATH = "feedback_ranker_meta.json"

FEATURES = [
    "priority_score", "financial_exposure", "match_confidence",
    "confidence_score", "anomaly_score", "pattern_confidence",
]

FEATURE_ALIASES = {
    "match_confidence": "match_confidence",
    "confidence_score": "confidence_score",
    "anomaly_score": "anomaly_score",
    "pattern_confidence": "pattern_confidence",
    "financial_exposure": "financial_exposure",
    "priority_score": "priority_score",
}


# ── Data access ───────────────────────────────────────────────────────

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def load_labelled_cases(db: Path = DB) -> pd.DataFrame:
    """Decision-annotated case features. NEEDS_REVIEW is excluded (unlabeled)."""
    with sqlite3.connect(db) as conn:
        if not _table_exists(conn, "investigation_cases_rag") or not _table_exists(conn, "review_decisions"):
            return pd.DataFrame()
        cols = _existing_columns(conn, "investigation_cases_rag", FEATURES)
        sel = ", ".join(f"c.{name}" for name in cols) if cols else "c.case_id"
        return pd.read_sql_query(
            f"""SELECT c.case_id, c.issue_type, c.vendor_code, {sel},
                       r.decision, r.reviewer, r.decided_at
                FROM investigation_cases_rag c
                JOIN review_decisions r ON c.case_id = r.case_id
                WHERE r.decision IN ('CONFIRM', 'REJECT')
                ORDER BY r.decided_at""",
            conn,
        )


def _existing_columns(conn: sqlite3.Connection, table: str, wanted: List[str]) -> List[str]:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return [c for c in wanted if c in present]


# ── Stage A: Bayesian-smoothed priors ─────────────────────────────────

def _smoothed_rate(group: pd.Series, global_rate: float, alpha: float = ALPHA) -> float:
    n = len(group)
    if n == 0:
        return global_rate
    confirms = float((group == "CONFIRM").sum())
    return (confirms + alpha * global_rate) / (n + alpha)


def compute_priors(db: Path = DB) -> Dict[str, Dict[str, float]]:
    """Smoothed confirm-rate priors by issue_type / vendor_code / pattern_id.

    Returns {"global": float, "by_issue": {...}, "by_vendor": {...}, "by_pattern": {...}}.
    """
    df = load_labelled_cases(db)
    global_rate = float((df.decision == "CONFIRM").mean()) if len(df) else 0.5
    priors: Dict[str, Dict[str, float]] = {"global": round(global_rate, 4)}

    if df.empty:
        return priors

    by_issue = {str(k): _smoothed_rate(g, global_rate) for k, g in df.groupby("issue_type")["decision"]}
    by_vendor = {str(k): _smoothed_rate(g, global_rate) for k, g in df.groupby("vendor_code")["decision"]}

    # pattern membership comes from the membership table
    pattern_labels: Dict[str, pd.Series] = {}
    try:
        with sqlite3.connect(db) as conn:
            if _table_exists(conn, "pattern_memberships"):
                mem = pd.read_sql_query("SELECT pattern_id, case_id FROM pattern_memberships", conn)
                merged = mem.merge(df, on="case_id", how="inner")
                pattern_labels = {pid: g["decision"] for pid, g in merged.groupby("pattern_id")}
    except Exception:
        pattern_labels = {}

    priors["by_issue"] = {k: round(v, 4) for k, v in by_issue.items()}
    priors["by_vendor"] = {k: round(v, 4) for k, v in by_vendor.items()}
    priors["by_pattern"] = {str(k): round(_smoothed_rate(v, global_rate), 4) for k, v in pattern_labels.items()}
    return priors


def _prior_for(case: pd.Series, priors: Dict[str, Dict[str, float]]) -> Tuple[float, str]:
    """Best (most specific, most data-backed) prior for a case, with its source."""
    global_rate = priors.get("global", 0.5)
    issue = str(case.get("issue_type", ""))
    vendor = str(case.get("vendor_code", ""))
    pid = str(case.get("pattern_ids", "")).split("|")[0] if case.get("pattern_ids") else ""

    candidates: List[Tuple[int, float, str]] = []
    if pid and pid in priors.get("by_pattern", {}):
        candidates.append((3, priors["by_pattern"][pid], f"pattern {pid}"))
    if vendor and vendor in priors.get("by_vendor", {}):
        candidates.append((2, priors["by_vendor"][vendor], f"vendor {vendor}"))
    if issue and issue in priors.get("by_issue", {}):
        candidates.append((1, priors["by_issue"][issue], f"issue type {issue}"))
    if not candidates:
        return global_rate, "no history (global prior)"
    # Most specific prior with data behind it wins.
    candidates.sort(key=lambda x: -x[0])
    specificity, rate, source = candidates[0]
    return rate, source


def stage_a_adjustment(cases: pd.DataFrame, priors: Dict[str, Dict[str, float]]) -> pd.DataFrame:
    """Add feedback_prior, feedback_source, feedback_adjustment columns."""
    out = cases.copy()
    if out.empty:
        for col in ("feedback_prior", "feedback_source", "feedback_adjustment"):
            out[col] = [] if col == "feedback_source" else 0.0
        return out
    rates, sources, adjustments = [], [], []
    global_rate = priors.get("global", 0.5)
    for _, case in out.iterrows():
        rate, source = _prior_for(case, priors)
        rates.append(rate)
        sources.append(source)
        # Adjust relative to neutral 0.5: a history of confirmations pushes up,
        # a history of rejections pushes down; capped and weighted.
        adj = round(max(-FEEDBACK_CAP, min(FEEDBACK_CAP, FEEDBACK_WEIGHT * 100 * (rate - 0.5) * 2)), 2)
        adjustments.append(adj)
    out["feedback_prior"] = rates
    out["feedback_source"] = sources
    out["feedback_adjustment"] = adjustments
    return out


# ── Stage B: xgboost P(confirmed) ─────────────────────────────────────

def _feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in FEATURES if c in df.columns]
    X = df[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    for c in FEATURES:
        if c not in X.columns:
            X[c] = 0.0
    return X[FEATURES]


def train_ranker(db: Path = DB, models_dir: Optional[Path] = None) -> Optional[dict]:
    """Train the stage-B classifier if enough labelled data exists.

    Returns metadata (or None when not activated). Metrics come from
    stratified 3-fold cross-validation; the final model is refit on all data.
    """
    df = load_labelled_cases(db)
    if len(df) < ML_MIN_LABELS:
        return None
    y = (df["decision"] == "CONFIRM").astype(int)
    if y.nunique() < 2:
        return None

    X = _feature_matrix(df)
    try:
        from xgboost import XGBClassifier
    except ImportError:
        return None

    metrics: Dict[str, float] = {}
    try:
        from sklearn.model_selection import StratifiedKFold, cross_val_predict
        min_class = int(y.value_counts().min())
        n_splits = max(2, min(3, min_class))
        clf_template = XGBClassifier(
            n_estimators=120, max_depth=3, learning_rate=0.1,
            subsample=0.9, colsample_bytree=0.9,
            eval_metric="logloss", random_state=42, n_jobs=1,
        )
        if min_class >= n_splits:
            cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
            probs = cross_val_predict(clf_template, X, y, cv=cv, method="predict_proba")[:, 1]
            metrics = _classification_metrics(y, probs)
    except Exception:
        metrics = {}

    model = XGBClassifier(
        n_estimators=120, max_depth=3, learning_rate=0.1,
        subsample=0.9, colsample_bytree=0.9,
        eval_metric="logloss", random_state=42, n_jobs=1,
    )
    model.fit(X, y)

    import json
    models_dir = Path(models_dir) if models_dir else config.MODELS_DIR
    models_dir.mkdir(parents=True, exist_ok=True)
    import joblib
    joblib.dump(model, models_dir / ML_MODEL_PATH)
    meta = {
        "trained_on": int(len(df)),
        "positives": int(y.sum()),
        "negatives": int((y == 0).sum()),
        "features": FEATURES,
        "cv_metrics": metrics,
        "min_labels_required": ML_MIN_LABELS,
    }
    (models_dir / ML_META_PATH).write_text(json.dumps(meta, indent=2))
    return meta


def _classification_metrics(y_true: pd.Series, probs) -> Dict[str, float]:
    from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
    probs = pd.Series(probs).clip(0.0, 1.0)
    out: Dict[str, float] = {}
    try:
        out["roc_auc"] = round(float(roc_auc_score(y_true, probs)), 4)
    except Exception:
        out["roc_auc"] = None
    try:
        out["log_loss"] = round(float(log_loss(y_true, probs)), 4)
    except Exception:
        out["log_loss"] = None
    try:
        out["brier"] = round(float(brier_score_loss(y_true, probs)), 4)
    except Exception:
        out["brier"] = None
    return {k: v for k, v in out.items() if v is not None}


def load_ranker(models_dir: Optional[Path] = None):
    """Return (model, meta) or (None, None) when no trained ranker exists."""
    import joblib, json
    models_dir = Path(models_dir) if models_dir else config.MODELS_DIR
    model_path = models_dir / ML_MODEL_PATH
    meta_path = models_dir / ML_META_PATH
    if not model_path.exists() or not meta_path.exists():
        return None, None
    try:
        model = joblib.load(model_path)
        meta = json.loads(meta_path.read_text())
        return model, meta
    except Exception:
        return None, None


def stage_b_scores(cases: pd.DataFrame, models_dir: Optional[Path] = None) -> pd.DataFrame:
    """Add ml_p_confirmed and ml_boost columns (0.0 when ranker inactive)."""
    out = cases.copy()
    model, meta = load_ranker(models_dir)
    if model is None or out.empty:
        out["ml_p_confirmed"] = None if not out.empty else []
        out["ml_boost"] = 0.0 if not out.empty else []
        return out
    X = _feature_matrix(out)
    try:
        probs = model.predict_proba(X)[:, 1]
    except Exception:
        out["ml_p_confirmed"] = None
        out["ml_boost"] = 0.0
        return out
    out["ml_p_confirmed"] = [round(float(p), 4) for p in probs]
    out["ml_boost"] = [round(max(-ML_CAP, min(ML_CAP, ML_WEIGHT * 100 * (float(p) - 0.5) * 2)), 2) for p in probs]
    return out


# ── Orchestration ─────────────────────────────────────────────────────

def apply_feedback_ranking(db: Path = DB, cases: pd.DataFrame | None = None) -> Tuple[pd.DataFrame, Dict]:
    """Blend stage A (+ stage B when active) into final priority scores.

    Returns (cases, info) where info documents what was applied so the API/UI
    can show 'why ranked here'. Ranking-only: status/decision fields untouched.
    """
    priors = compute_priors(db)
    if cases is None:
        with sqlite3.connect(db) as conn:
            if not _table_exists(conn, "investigation_cases_rag"):
                return pd.DataFrame(), {"priors": priors, "ml_active": False, "note": "no cases table"}
            cases = pd.read_sql_query("SELECT * FROM investigation_cases_rag", conn)

    out = stage_a_adjustment(cases, priors)
    out = stage_b_scores(out)
    ml_active = out["ml_p_confirmed"].notna().any() if not out.empty else False

    base_col = "final_priority_score" if "final_priority_score" in out.columns else "priority_score"
    out["base_priority_score"] = out[base_col]
    out["final_priority_score"] = (
        out[base_col] + out["feedback_adjustment"].fillna(0.0) + out["ml_boost"].fillna(0.0)
    ).clip(0, 100).round(2)

    def explain(row: pd.Series) -> str:
        parts = [f"base {row['base_priority_score']:.1f}"]
        fb = row.get("feedback_adjustment", 0.0) or 0.0
        if fb:
            src = row.get("feedback_source", "")
            parts.append(f"feedback {fb:+.1f} ({src})")
        boost = row.get("ml_boost", 0.0) or 0.0
        if boost and row.get("ml_p_confirmed") is not None:
            parts.append(f"ML P(confirmed)={row['ml_p_confirmed']:.2f} → {boost:+.1f}")
        if len(parts) == 1:
            parts.append("no feedback history yet")
        return "; ".join(parts)

    out["ranking_explanation"] = out.apply(explain, axis=1)

    info = {
        "priors": priors,
        "ml_active": bool(ml_active),
        "feedback_adjusted_cases": int((out["feedback_adjustment"].fillna(0) != 0).sum()) if not out.empty else 0,
        "weight_cap": {"feedback": FEEDBACK_CAP, "ml": ML_CAP},
    }
    return out, info


RANKING_COLUMNS = {
    "final_priority_score": "REAL", "ranking_explanation": "TEXT",
    "feedback_adjustment": "REAL", "feedback_source": "TEXT",
    "ml_p_confirmed": "REAL", "ml_boost": "REAL",
}


def _ensure_ranking_columns(conn: sqlite3.Connection) -> None:
    present = {row[1] for row in conn.execute("PRAGMA table_info(investigation_cases_rag)")}
    for col, col_type in RANKING_COLUMNS.items():
        if col not in present:
            conn.execute(f"ALTER TABLE investigation_cases_rag ADD COLUMN {col} {col_type}")


def refresh_ranking_in_db(db: Path = DB) -> Dict:
    """Recompute feedback-adjusted ranking for all cases in the DB."""
    cases, info = apply_feedback_ranking(db)
    if cases.empty:
        return info
    with sqlite3.connect(db) as conn:
        _ensure_ranking_columns(conn)
        # Keep priority bands consistent with the feedback-adjusted score.
        def _band(score: float) -> str:
            if score >= 80:
                return "CRITICAL"
            if score >= 60:
                return "HIGH"
            if score >= 35:
                return "MEDIUM"
            return "LOW"

        conn.executemany(
            """UPDATE investigation_cases_rag
               SET final_priority_score = ?, ranking_explanation = ?,
                   feedback_adjustment = ?, feedback_source = ?,
                   ml_p_confirmed = ?, ml_boost = ?
               WHERE case_id = ?""",
            [
                (float(r["final_priority_score"]), str(r["ranking_explanation"]),
                 float(r.get("feedback_adjustment") or 0.0), str(r.get("feedback_source") or ""),
                 None if r.get("ml_p_confirmed") is None or (isinstance(r.get("ml_p_confirmed"), float) and math.isnan(r.get("ml_p_confirmed"))) else float(r["ml_p_confirmed"]),
                 float(r.get("ml_boost") or 0.0), str(r["case_id"]))
                for _, r in cases.iterrows()
            ],
        )
        conn.executemany(
            "UPDATE investigation_cases_rag SET priority_band = ? WHERE case_id = ?",
            [(_band(float(r["final_priority_score"])), str(r["case_id"])) for _, r in cases.iterrows()],
        )
        conn.commit()
    return info


def feedback_summary(db: Path = DB) -> dict:
    """Dashboard feedback panel data."""
    from .human_review import init_review_tables
    init_review_tables(db)
    with sqlite3.connect(db) as conn:
        if not _table_exists(conn, "review_decisions"):
            return {"reviewed_cases": 0}
        total = conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
        by_decision = dict(conn.execute("SELECT decision, COUNT(*) FROM review_decisions GROUP BY decision").fetchall())
        by_issue = pd.read_sql_query(
            """SELECT c.issue_type, r.decision, COUNT(*) AS n
               FROM review_decisions r JOIN investigation_cases_rag c ON c.case_id = r.case_id
               GROUP BY c.issue_type, r.decision""", conn,
        ) if _table_exists(conn, "investigation_cases_rag") else pd.DataFrame()
    result = {
        "reviewed_cases": int(total),
        "by_decision": {str(k): int(v) for k, v in by_decision.items()},
        "stage_b_active": bool(load_ranker()[0] is not None),
        "stage_b_min_labels": ML_MIN_LABELS,
    }
    if not by_issue.empty:
        pivot = by_issue.pivot_table(index="issue_type", columns="decision", values="n", fill_value=0)
        result["by_issue"] = {str(k): {str(c): int(v) for c, v in row.items()} for k, row in pivot.iterrows()}
    return result
