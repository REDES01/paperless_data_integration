"""
Configuration constants for the behavior emulator.

Environment-overridable so you can speed up the bots for demo recording
(BE_MODE=demo → 10x rates) or slow them down for a long unattended run
(BE_MODE=slow → 0.1x rates).
"""
import os

# ── Mode selector ────────────────────────────────────────────────────────
# normal: realistic rates, can run for days without overloading the system
# demo:   10x rates, generates visible activity in 10-15 min of recording
# slow:   0.1x rates, minimal load for long-duration stability tests
# off:    bots stay alive but sleep — useful if you need to pause traffic
MODE = os.environ.get("BE_MODE", "normal").lower()
_rate_multiplier = {"off": 0.0, "slow": 0.1, "normal": 1.0, "demo": 10.0}[MODE]

# Base rates — corrections + searches per MINUTE
CORRECTIONS_PER_MIN = 3.0 * _rate_multiplier
SEARCHES_PER_MIN    = 2.0 * _rate_multiplier

# ── Connection config ────────────────────────────────────────────────────
DB_DSN = os.environ.get(
    "DB_DSN",
    "host=postgres port=5432 dbname=paperless user=user password=paperless_postgres",
)
ML_GATEWAY_URL = os.environ.get("ML_GATEWAY_URL", "http://ml_gateway:8000")

# ── Correction persona weights ──────────────────────────────────────────
# Careful: reads the image, fixes most typos — produces meaningful corrections
# Sloppy:  fixes some typos, leaves others — realistic mid-effort user
# Lazy:    hits Save without editing — exercises the R1_no_op quality filter
#
# The weights approximately match a real user population: most people are
# careful about a task they opted into, some are lazy.
PERSONAS = {
    "careful": {"weight": 0.6, "fix_rate": 0.9, "noop_rate": 0.02},
    "sloppy":  {"weight": 0.3, "fix_rate": 0.5, "noop_rate": 0.10},
    "lazy":    {"weight": 0.1, "fix_rate": 0.1, "noop_rate": 0.70},
}

# ── Query pool + relevance map ───────────────────────────────────────────
# Each query has a set of keywords that, if present in a result doc's
# merged_text snippet, mark the result as "relevant" to this query. The
# search bot biases its feedback toward thumbs_up on relevant results
# and thumbs_down on irrelevant ones.
#
# This gives the reranker a stable, learnable target: over many
# emulator iterations, relevant docs get positive feedback and rise in
# rank; irrelevant ones get negative and fall.
QUERY_POOL = [
    {"query": "budget analysis",         "keywords": ["budget", "fiscal", "financial", "funding", "allocation"]},
    {"query": "quarterly review",        "keywords": ["quarter", "Q1", "Q2", "Q3", "Q4", "review"]},
    {"query": "meeting notes",           "keywords": ["meeting", "agenda", "discussion", "minutes", "attendees"]},
    {"query": "faculty hiring",          "keywords": ["faculty", "hire", "recruit", "tenure", "position"]},
    {"query": "travel reimbursement",    "keywords": ["travel", "reimburse", "expense", "trip", "per diem"]},
    {"query": "research grant",          "keywords": ["grant", "research", "nsf", "funding", "proposal"]},
    {"query": "student enrollment",      "keywords": ["student", "enroll", "registration", "admission", "cohort"]},
    {"query": "conference support",      "keywords": ["conference", "travel", "support", "presentation"]},
    {"query": "curriculum changes",      "keywords": ["curriculum", "course", "syllabus", "program", "changes"]},
    {"query": "laboratory equipment",    "keywords": ["lab", "equipment", "instrument", "purchase", "facility"]},
    {"query": "graduate admissions",     "keywords": ["graduate", "admission", "applicant", "phd", "masters"]},
    {"query": "safety protocols",        "keywords": ["safety", "protocol", "hazard", "incident", "compliance"]},
    {"query": "publication deadline",    "keywords": ["publication", "deadline", "journal", "submit", "manuscript"]},
    {"query": "equipment maintenance",   "keywords": ["maintenance", "repair", "service", "equipment", "replacement"]},
    {"query": "sabbatical planning",     "keywords": ["sabbatical", "leave", "research", "plan", "approval"]},
    {"query": "course evaluation",       "keywords": ["evaluation", "feedback", "course", "teaching", "survey"]},
]

# Feedback probability tables.
# When a result IS relevant (query keywords appear in snippet):
P_FEEDBACK_RELEVANT = {"thumbs_up": 0.50, "thumbs_down": 0.05, "click": 0.15, "ignore": 0.30}
# When a result is NOT relevant:
P_FEEDBACK_IRRELEVANT = {"thumbs_up": 0.05, "thumbs_down": 0.40, "click": 0.10, "ignore": 0.45}

# ── Search parameters ────────────────────────────────────────────────────
SEARCH_K = 5       # top-k results to fetch from ml_gateway per query

# ── Correction parameters ────────────────────────────────────────────────
CORRECTIONS_BATCH = 3   # regions to correct per loop iteration
MIN_HTR_LEN = 2         # skip regions where htr_output is too short to perturb meaningfully
