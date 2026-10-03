from __future__ import annotations

from pathlib import Path
import math
import sqlite3
from typing import Dict, List, Tuple

import networkx as nx
import pandas as pd

from . import config

ROOT = config.ROOT
DATA = config.DATA_DIR


def _safe_float(v, default=0.0):
    try:
        x = float(v)
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


def _confidence(n: int, strength: float) -> float:
    """Pattern confidence formula: 45 + 4×min(n,10) + 25×concentration, capped 99.

    Recurrence count dominates; concentration (share of the vendor's or the
    period's cases affected) adds up to 25 points.
    """
    return round(min(99.0, 45.0 + min(n, 10) * 4.0 + min(max(strength, 0.0), 1.0) * 25.0), 2)


def _trend(grp: pd.DataFrame) -> str:
    """Is the vendor's error rate rising? Compare first half vs second half of
    the group's case dates. RISING / STABLE / FALLING / UNKNOWN."""
    dates = pd.to_datetime(grp.get("invoice_date"), errors="coerce").dropna().sort_values()
    if len(dates) < 4:
        return "UNKNOWN"
    mid = len(dates) // 2
    first, second = dates.iloc[:mid], dates.iloc[mid:]
    span_first = max(1, (first.max() - first.min()).days)
    span_second = max(1, (second.max() - second.min()).days)
    rate_first = len(first) / span_first
    rate_second = len(second) / span_second
    if rate_second > rate_first * 1.5:
        return "RISING"
    if rate_first > rate_second * 1.5:
        return "FALLING"
    return "STABLE"


def build_relationship_graph(cases: pd.DataFrame) -> nx.Graph:
    """Create a lightweight Vendor <-> Issue <-> Invoice relationship graph."""
    g = nx.Graph()
    for _, r in cases.iterrows():
        vendor = str(r.get("vendor_code", "")).strip()
        issue = str(r.get("issue_type", "")).strip()
        invoice = str(r.get("invoice_id", "")).strip()
        if vendor and vendor != "nan":
            g.add_node(f"vendor:{vendor}", kind="vendor", label=vendor)
        if issue and issue != "nan":
            g.add_node(f"issue:{issue}", kind="issue", label=issue)
        if invoice and invoice != "nan":
            g.add_node(f"invoice:{invoice}", kind="invoice", label=invoice)
        if vendor and issue and vendor != "nan" and issue != "nan":
            g.add_edge(f"vendor:{vendor}", f"issue:{issue}", relation="has_issue")
        if vendor and invoice and vendor != "nan" and invoice != "nan":
            g.add_edge(f"vendor:{vendor}", f"invoice:{invoice}", relation="booked")
        if invoice and issue and invoice != "nan" and issue != "nan":
            g.add_edge(f"invoice:{invoice}", f"issue:{issue}", relation="flagged_as")
    return g


def detect_patterns(cases: pd.DataFrame, invoices: pd.DataFrame | None = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Detect recurring discrepancy patterns and return pattern catalog + case memberships."""
    df = cases.copy()
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    if invoices is not None and "vendor_code" not in df.columns and "invoice_id" in df.columns:
        cols = [c for c in ["invoice_id", "vendor_code", "invoice_date"] if c in invoices.columns]
        df = df.merge(invoices[cols].drop_duplicates("invoice_id"), on="invoice_id", how="left")
    if "vendor_code" not in df.columns:
        df["vendor_code"] = "UNKNOWN"
    if "invoice_date" not in df.columns:
        df["invoice_date"] = ""
    df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce")
    df["period"] = df["invoice_date"].dt.to_period("M").astype(str).replace("NaT", "UNKNOWN")

    patterns: List[Dict] = []
    memberships: List[Dict] = []
    pattern_no = 1

    # 1) Vendor + issue recurrence: strongest investigation signal.
    for (vendor, issue), grp in df.groupby(["vendor_code", "issue_type"], dropna=False):
        n = len(grp)
        if n < 3:
            continue
        total_vendor = max(1, int((df["vendor_code"] == vendor).sum()))
        concentration = n / total_vendor
        pid = f"PAT-{pattern_no:04d}"; pattern_no += 1
        label = f"Recurring {str(issue).replace('_', ' ')} pattern for vendor {vendor}"
        patterns.append({
            "pattern_id": pid, "pattern_type": "VENDOR_ISSUE_RECURRENCE", "pattern_label": label,
            "vendor_code": vendor, "issue_type": issue, "period": "MULTI-PERIOD",
            "occurrence_count": n, "affected_vendors": 1, "affected_invoices": grp["invoice_id"].nunique(),
            "concentration": round(concentration, 4),
            "pattern_confidence": _confidence(n, concentration),
            "financial_exposure": round(float(grp.get("financial_exposure", pd.Series(dtype=float)).apply(_safe_float).sum()), 2),
            "trend": _trend(grp),
            "first_seen": pd.to_datetime(grp["invoice_date"], errors="coerce").min().date().isoformat() if grp["invoice_date"].notna().any() else None,
            "last_seen": pd.to_datetime(grp["invoice_date"], errors="coerce").max().date().isoformat() if grp["invoice_date"].notna().any() else None,
            "explanation": f"{n} cases of the same discrepancy type are associated with vendor {vendor}.",
        })
        for _, r in grp.iterrows():
            memberships.append({"pattern_id": pid, "case_id": r["case_id"], "membership_reason": "same vendor + discrepancy type"})

    # 2) Cross-vendor issue recurrence in a month: identifies systemic patterns.
    for (period, issue), grp in df.groupby(["period", "issue_type"], dropna=False):
        vendors = grp["vendor_code"].nunique()
        n = len(grp)
        if n < 4 or vendors < 2:
            continue
        concentration = vendors / max(1, df.loc[df["period"] == period, "vendor_code"].nunique())
        pid = f"PAT-{pattern_no:04d}"; pattern_no += 1
        patterns.append({
            "pattern_id": pid, "pattern_type": "CROSS_VENDOR_PERIOD",
            "pattern_label": f"Recurring {str(issue).replace('_', ' ')} across vendors in {period}",
            "vendor_code": "MULTIPLE", "issue_type": issue, "period": period,
            "occurrence_count": n, "affected_vendors": vendors, "affected_invoices": grp["invoice_id"].nunique(),
            "concentration": round(concentration, 4), "pattern_confidence": _confidence(n, concentration),
            "financial_exposure": round(float(grp.get("financial_exposure", pd.Series(dtype=float)).apply(_safe_float).sum()), 2),
            "trend": _trend(grp),
            "first_seen": pd.to_datetime(grp["invoice_date"], errors="coerce").min().date().isoformat() if grp["invoice_date"].notna().any() else None,
            "last_seen": pd.to_datetime(grp["invoice_date"], errors="coerce").max().date().isoformat() if grp["invoice_date"].notna().any() else None,
            "explanation": f"{n} cases across {vendors} vendors share the same discrepancy type during {period}.",
        })
        for _, r in grp.iterrows():
            memberships.append({"pattern_id": pid, "case_id": r["case_id"], "membership_reason": "same period + discrepancy type"})

    # 3) Repeated anomaly/issue combination if ML enrichment is present.
    if "ml_anomaly" in df.columns:
        for vendor, grp in df[df["ml_anomaly"] == True].groupby("vendor_code"):
            n = len(grp)
            if n < 3:
                continue
            pid = f"PAT-{pattern_no:04d}"; pattern_no += 1
            concentration = n / max(1, len(df[df["vendor_code"] == vendor]))
            patterns.append({
                "pattern_id": pid, "pattern_type": "RECURRING_ML_ANOMALY",
                "pattern_label": f"Repeated anomalous transaction pattern for vendor {vendor}",
                "vendor_code": vendor, "issue_type": "ML_ANOMALY", "period": "MULTI-PERIOD",
                "occurrence_count": n, "affected_vendors": 1, "affected_invoices": grp["invoice_id"].nunique(),
                "concentration": round(concentration, 4), "pattern_confidence": _confidence(n, concentration),
                "financial_exposure": round(float(grp.get("financial_exposure", pd.Series(dtype=float)).apply(_safe_float).sum()), 2),
                "trend": _trend(grp),
                "first_seen": pd.to_datetime(grp["invoice_date"], errors="coerce").min().date().isoformat() if grp["invoice_date"].notna().any() else None,
                "last_seen": pd.to_datetime(grp["invoice_date"], errors="coerce").max().date().isoformat() if grp["invoice_date"].notna().any() else None,
                "explanation": f"{n} ML-anomalous transactions are associated with vendor {vendor}.",
            })
            for _, r in grp.iterrows():
                memberships.append({"pattern_id": pid, "case_id": r["case_id"], "membership_reason": "repeated ML anomaly for vendor"})

    patterns_df = pd.DataFrame(patterns)
    memberships_df = pd.DataFrame(memberships)
    if not patterns_df.empty:
        patterns_df = patterns_df.sort_values(["pattern_confidence", "financial_exposure"], ascending=False).reset_index(drop=True)
        # Two-way linkage: patterns carry their member case ids; membership rows
        # already carry the pattern id.
        member_map = memberships_df.groupby("pattern_id")["case_id"].apply(lambda s: ",".join(sorted(set(map(str, s))))) if not memberships_df.empty else {}
        patterns_df["member_case_ids"] = patterns_df["pattern_id"].map(member_map).fillna("")
    else:
        patterns_df = pd.DataFrame(columns=PATTERN_COLUMNS)
    if memberships_df.empty:
        memberships_df = pd.DataFrame(columns=MEMBERSHIP_COLUMNS)
    return patterns_df, memberships_df


def enrich_cases_with_patterns(cases: pd.DataFrame, patterns: pd.DataFrame, memberships: pd.DataFrame) -> pd.DataFrame:
    out = cases.copy()
    if patterns.empty or memberships.empty:
        out["pattern_count"] = 0
        out["pattern_ids"] = ""
        out["pattern_signal"] = "NO_RECURRING_PATTERN"
        out["pattern_confidence"] = 0.0
        out["pattern_explanation"] = "No recurring discrepancy pattern detected."
        return out
    pcols = ["pattern_id", "pattern_label", "pattern_confidence", "explanation"]
    m = memberships.merge(patterns[pcols], on="pattern_id", how="left")
    agg = m.groupby("case_id").agg(
        pattern_count=("pattern_id", "nunique"),
        pattern_ids=("pattern_id", lambda s: "|".join(sorted(set(map(str, s))))),
        pattern_confidence=("pattern_confidence", "max"),
        pattern_explanation=("explanation", lambda s: " | ".join(dict.fromkeys(map(str, s))))
    ).reset_index()
    out = out.merge(agg, on="case_id", how="left")
    out["pattern_count"] = out["pattern_count"].fillna(0).astype(int)
    out["pattern_ids"] = out["pattern_ids"].fillna("")
    out["pattern_confidence"] = out["pattern_confidence"].fillna(0.0).round(2)
    out["pattern_signal"] = out["pattern_count"].gt(0).map({True: "RECURRING_PATTERN", False: "NO_RECURRING_PATTERN"})
    out["pattern_explanation"] = out["pattern_explanation"].fillna("No recurring discrepancy pattern detected.")
    out["pattern_priority_boost"] = (out["pattern_confidence"] * 0.10).round(2)
    base = out["combined_priority_score"] if "combined_priority_score" in out.columns else out["priority_score"]
    out["final_priority_score"] = (base + out["pattern_priority_boost"]).clip(upper=100).round(2)
    return out


PATTERN_COLUMNS = [
    "pattern_id", "pattern_type", "pattern_label", "vendor_code", "issue_type", "period",
    "occurrence_count", "affected_vendors", "affected_invoices", "concentration",
    "pattern_confidence", "financial_exposure", "trend", "first_seen", "last_seen",
    "member_case_ids", "explanation",
]
MEMBERSHIP_COLUMNS = ["pattern_id", "case_id", "membership_reason"]


def save_pattern_outputs(patterns: pd.DataFrame, memberships: pd.DataFrame, cases: pd.DataFrame, data_dir: Path | None = None, db_path: Path | None = None) -> None:
    data_dir = Path(data_dir) if data_dir else config.DATA_DIR
    db_path = Path(db_path) if db_path else (config.DB_PATH if data_dir == config.DATA_DIR else data_dir / "reconai.db")
    # Empty pattern results must still produce valid (empty) tables.
    if patterns.empty:
        patterns = pd.DataFrame(columns=PATTERN_COLUMNS)
    if memberships.empty:
        memberships = pd.DataFrame(columns=MEMBERSHIP_COLUMNS)
    patterns.to_csv(data_dir / "error_patterns.csv", index=False)
    memberships.to_csv(data_dir / "pattern_memberships.csv", index=False)
    cases.to_csv(data_dir / "investigation_cases_patterned.csv", index=False)
    with sqlite3.connect(db_path) as conn:
        patterns.to_sql("error_patterns", conn, if_exists="replace", index=False)
        memberships.to_sql("pattern_memberships", conn, if_exists="replace", index=False)
        cases.to_sql("investigation_cases_patterned", conn, if_exists="replace", index=False)
        conn.commit()


def run_and_save(data_dir: Path = DATA):
    cases = pd.read_csv(data_dir / "investigation_cases_enriched.csv")
    invoices = pd.read_csv(data_dir / "invoices.csv")
    patterns, memberships = detect_patterns(cases, invoices)
    patterned = enrich_cases_with_patterns(cases, patterns, memberships)
    save_pattern_outputs(patterns, memberships, patterned, data_dir)
    return patterns, memberships, patterned


if __name__ == "__main__":
    p, m, c = run_and_save()
    print(f"Patterns detected: {len(p)}")
    print(f"Pattern memberships: {len(m)}")
    print(f"Cases linked to patterns: {(c['pattern_count'] > 0).sum() if not c.empty else 0}")
