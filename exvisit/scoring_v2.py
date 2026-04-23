"""Scoring v2 — log-linear multi-signal ranker for ExVisit anchor selection.

Designed to maximize P(arg-max == oracle) while remaining deterministic and
model-agnostic. Reads precomputed graph metadata from the `.meta.json` sidecar
written by `scaffold.generate(..., meta_out=...)`.

Score components (all O(1) per node after preprocessing):

    S(n|q) = β_lex   · BM25(q, n_tokens) / Z_bm25
           + β_trace · trace_overlap(q.trace, n.src_path, n.line_range)
           + β_sym   · max_symbol_overlap(q.code_terms, n.symbols)
           + β_cent  · log(1 + pagerank(n))
           + β_idf   · log(1 + 1/cluster_size(n))
           + β_reg   · 1[n.kind == 'registry']
           + β_stem  · stem_in_text(n, q)
           + β_path  · path_term_match(n, q)
           - β_test  · test_gate(n, q)

Anchor selection is `softmax(S/T)` with margin test → either single argmax or
top-K candidates when low-confidence.
"""
from __future__ import annotations

import ast
import json
import math
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .ast import Node, exvisitDoc
from .graph_meta import GraphMeta


DEFAULT_BETAS_PATH = Path(__file__).resolve().parent.parent / "config" / "blast_betas.json"


@dataclass
class ScoredNode:
    score: float
    node: Node
    components: Dict[str, float]
    reasons: List[str]


@dataclass
class V2Config:
    betas: Dict[str, float]
    test_admit_terms: List[str]
    migration_admit_terms: List[str]
    config_admit_terms: List[str]
    anchor_margin: float
    topk: int
    softmax_temp: float


def load_v2_config(path: Optional[Path] = None) -> V2Config:
    p = path or DEFAULT_BETAS_PATH
    payload = json.loads(p.read_text(encoding="utf-8"))
    betas = {str(k): float(v) for k, v in payload["betas"].items()}
    th = payload.get("thresholds", {})
    return V2Config(
        betas=betas,
        test_admit_terms=[t.lower() for t in th.get("test_admit_terms", [])],
        migration_admit_terms=[t.lower() for t in th.get("migration_admit_terms", [])],
        config_admit_terms=[t.lower() for t in th.get("config_admit_terms", [])],
        anchor_margin=float(th.get("anchor_margin", 0.10)),
        topk=int(th.get("topk", 3)),
        softmax_temp=float(th.get("softmax_temp", 0.6)),
    )


# ---------------------------------------------------------------------------
# Tokenization helpers (BM25 over identifier-aware tokens)
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_")
_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
    "for", "from", "get", "if", "in", "into", "is", "it", "its", "not",
    "of", "on", "or", "should", "that", "the", "their", "this", "to",
    "when", "with", "without", "while", "use", "used", "uses", "self",
    "test", "tests", "issue", "bug", "fix", "error", "exception",
    "traceback", "case", "expected", "actual",
}


def _tokenize_text(text: str) -> List[str]:
    out: List[str] = []
    for tok in _TOKEN_RE.findall(text):
        low = tok.lower()
        # also split CamelCase / snake_case
        parts = [p.lower() for p in _CAMEL_SPLIT.split(tok) if p]
        for p in (low, *parts):
            if len(p) <= 2 or p in _STOP:
                continue
            out.append(p)
    return out


def _node_token_bag(node: Node, symbols: Sequence[str]) -> List[str]:
    bits: List[str] = []
    if node.src_path:
        # path components and stem
        for part in re.split(r"[\\/]", node.src_path):
            bits.extend(_tokenize_text(part))
    bits.extend(_tokenize_text(node.name))
    for s in symbols:
        bits.extend(_tokenize_text(s))
    return bits


# ---------------------------------------------------------------------------
# BM25 (corpus-level IDF computed once per scoring call)
# ---------------------------------------------------------------------------
def _bm25_score(
    q_tokens: Sequence[str],
    doc_tokens: Sequence[str],
    idf: Dict[str, float],
    avg_dl: float,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    if not doc_tokens:
        return 0.0
    dl = len(doc_tokens)
    tf = Counter(doc_tokens)
    score = 0.0
    for q in q_tokens:
        if q not in tf:
            continue
        f = tf[q]
        i = idf.get(q, 0.0)
        if i <= 0.0:
            continue
        denom = f + k1 * (1.0 - b + b * dl / max(avg_dl, 1.0))
        score += i * (f * (k1 + 1.0)) / denom
    return score


def _build_idf(corpus_token_bags: Sequence[Sequence[str]]) -> Tuple[Dict[str, float], float]:
    N = len(corpus_token_bags)
    df: Counter = Counter()
    total_dl = 0
    for bag in corpus_token_bags:
        total_dl += len(bag)
        for term in set(bag):
            df[term] += 1
    avg_dl = total_dl / max(N, 1)
    idf: Dict[str, float] = {}
    for term, n in df.items():
        # BM25 idf with +0.5 smoothing
        idf[term] = math.log((N - n + 0.5) / (n + 0.5) + 1.0)
    return idf, avg_dl


# ---------------------------------------------------------------------------
# Trace / symbol helpers
# ---------------------------------------------------------------------------
_TRACE_PATH_RE = re.compile(
    r'(?:File "(?P<p1>[^"\n]+)", line (?P<l1>\d+))'
    r'|(?P<p2>(?:[A-Za-z]:[\\/])?[^:\s"\']+?\.py):(?P<l2>\d+)'
)


@dataclass
class _Frame:
    path: str
    line: int


def _extract_frames(text: str) -> List[_Frame]:
    out: List[_Frame] = []
    seen: Set[Tuple[str, int]] = set()
    for m in _TRACE_PATH_RE.finditer(text):
        path = (m.group("p1") or m.group("p2") or "").replace("\\", "/")
        ln = m.group("l1") or m.group("l2")
        if not path or not ln:
            continue
        key = (path.lower(), int(ln))
        if key in seen:
            continue
        seen.add(key)
        out.append(_Frame(path=path, line=int(ln)))
    return out


def _trace_overlap(node: Node, frames: Sequence[_Frame]) -> float:
    if not frames or not node.src_path:
        return 0.0
    np = node.src_path.replace("\\", "/").lower()
    score = 0.0
    for f in frames:
        fp = f.path.lower()
        # path match (full suffix or basename)
        if fp.endswith(np) or np.endswith(fp):
            score += 1.0
            if node.line_range and node.line_range[0] <= f.line <= node.line_range[1]:
                score += 1.5
        elif Path(fp).name == Path(np).name:
            score += 0.5
    return score


def _max_symbol_overlap(q_terms: Sequence[str], symbols: Sequence[str]) -> float:
    """Max symbol-level alignment: returns in [0,1]."""
    if not symbols or not q_terms:
        return 0.0
    q_low = {t.lower() for t in q_terms}
    q_tails = {t.lower().split(".")[-1] for t in q_terms}
    best = 0.0
    for s in symbols:
        sl = s.lower()
        if sl in q_low:
            return 1.0
        if sl in q_tails:
            best = max(best, 0.85)
        else:
            # CamelCase containment
            for q in q_low:
                if q and (q in sl or sl in q):
                    best = max(best, 0.5)
    return best


def _stem_in_text(node: Node, q_lower: str) -> float:
    if not node.src_path:
        return 0.0
    stem = Path(node.src_path).stem.lower()
    if not stem or stem == "__init__":
        return 0.0
    if len(stem) <= 3:
        return 0.0
    return 1.0 if stem in q_lower else 0.0


def _path_term_match(node: Node, code_terms: Sequence[str]) -> float:
    if not node.src_path:
        return 0.0
    np = node.src_path.replace("\\", "/").lower()
    name = Path(np).name
    for term in code_terms:
        low = term.lower()
        if low.endswith(".py") and (np.endswith(low) or name == Path(low).name):
            return 1.0
    return 0.0


def _is_test_path(node: Node, kind: str) -> bool:
    if kind == "test":
        return True
    if not node.src_path:
        return False
    p = node.src_path.replace("\\", "/").lower()
    return "/tests/" in p or p.startswith("tests/") or Path(p).name.startswith("test_")


def _test_gate(node: Node, kind: str, q_lower: str, admit_terms: Sequence[str],
               trace_into_node: bool) -> float:
    """Returns 1.0 (penalty active) or 0.0 (admitted)."""
    if not _is_test_path(node, kind):
        return 0.0
    if trace_into_node:
        return 0.0
    for t in admit_terms:
        if t in q_lower:
            return 0.0
    return 1.0


_MIGRATION_FILE_RE = re.compile(r"\b\d{4}_[a-z][a-z0-9_]+\.py\b", re.IGNORECASE)
_MIGRATION_FILE_PATH_RE = re.compile(r"/migrations/\d{4}_[a-z][a-z0-9_]+\.py$", re.IGNORECASE)


def _is_generated_migration(node: Node, kind: str) -> bool:
    """True only for actual NUMBERED migration files (e.g. ``0001_initial.py``).

    Framework code in ``django/db/migrations/`` (autodetector.py, serializer.py,
    executor.py, loader.py, operations/*.py) is NOT a generated migration
    and must never be gated.
    """
    sp = (node.src_path or "").replace("\\", "/")
    if _MIGRATION_FILE_PATH_RE.search(sp):
        return True
    # Fallback for kind hint, but require the numeric prefix anyway
    if kind == "migration":
        name = sp.rsplit("/", 1)[-1]
        if re.match(r"^\d{4}_[a-z]", name, re.IGNORECASE):
            return True
    return False


def _migration_gate(node: Node, kind: str, q_lower: str,
                    admit_terms: Sequence[str],
                    trace_into_node: bool) -> float:
    """Returns 1.0 (penalty active) or 0.0 (admitted).

    v2.3: Only numbered migration files (``\\d{4}_*.py``) are eligible for
    the gate — the migrations framework itself (serializer, autodetector,
    executor, loader, operations) is regular library code and must rank
    on its own merits.

    Admitted (no penalty) when:
      - trace points into this node
      - issue text references an actual numbered migration filename
      - issue text mentions ``schema_editor`` or ``/migrations/`` literally
    """
    if not _is_generated_migration(node, kind):
        return 0.0
    if trace_into_node:
        return 0.0
    if _MIGRATION_FILE_RE.search(q_lower):
        return 0.0
    for t in admit_terms:
        if t in q_lower:
            return 0.0
    return 1.0


# ---------------------------------------------------------------------------
# Issue-text mining: explicit paths, quoted symbols, qualified references
# ---------------------------------------------------------------------------
_EXPLICIT_PATH_RE = re.compile(
    r"(?:(?:[a-z_][a-z0-9_]*/){1,}[a-z_][a-z0-9_]*\.py)"
    r"|(?:[a-z_][a-z0-9_]*/[a-z_][a-z0-9_]*(?:/[a-z_][a-z0-9_]*)+)",
    re.IGNORECASE,
)


def _extract_explicit_paths(text: str) -> List[str]:
    """Extract posix-style paths to .py files or package dirs mentioned in
    the issue text (e.g. 'django/db/models/lookups.py')."""
    out: List[str] = []
    seen: Set[str] = set()
    for m in _EXPLICIT_PATH_RE.findall(text):
        low = m.lower().replace("\\", "/")
        if low not in seen:
            seen.add(low)
            out.append(low)
    return out


def _explicit_path_match(node: Node, explicit_paths: Sequence[str]) -> float:
    """Strongest signal: node's src_path matches a path literally quoted in
    the issue. Binary 1.0/0.0. Overrides everything."""
    if not explicit_paths or not node.src_path:
        return 0.0
    np = node.src_path.replace("\\", "/").lower()
    for ep in explicit_paths:
        ep_l = ep.lstrip("./").lower()
        if np == ep_l or np.endswith("/" + ep_l) or np.endswith(ep_l):
            return 1.0
        # directory-level match (e.g. "django/views/")
        if not ep_l.endswith(".py") and ("/" + ep_l + "/") in ("/" + np + "/"):
            return 0.5
    return 0.0


def _symbol_exact_match(node: Node, symbols: Sequence[str],
                        code_terms: Sequence[str]) -> float:
    """Exact-match boost: any node symbol equals (case-insensitive) any
    issue code-term. Pure match signal, immune to BM25 vocabulary starvation.
    Returns a weighted hit count. Rare symbols (CamelCase, length > 8)
    count more than common ones (short, generic).
    """
    if not symbols or not code_terms:
        return 0.0
    sym_low = {s.lower(): s for s in symbols}
    seen_hits: Set[str] = set()
    total = 0.0
    for t in code_terms:
        tl = t.lower()
        # exact symbol match
        if tl in sym_low and tl not in seen_hits:
            seen_hits.add(tl)
            orig = sym_low[tl]
            # distinctiveness: longer + CamelCase + contains specific bigrams = more weight
            w = 1.0
            if len(orig) >= 8:
                w += 0.5
            if any(c.isupper() for c in orig[1:]):  # CamelCase
                w += 0.5
            # generic symbols (Field, Model, Form, View) get discounted
            if orig in {"Field", "Model", "Form", "View", "Meta", "Manager",
                        "Query", "QuerySet", "Error", "Exception"}:
                w *= 0.4
            total += w
            continue
        # dotted form (Class.method) — match each segment
        if "." in tl:
            for part in tl.split("."):
                if part and part in sym_low and part not in seen_hits:
                    seen_hits.add(part)
                    orig = sym_low[part]
                    w = 1.0
                    if len(orig) >= 8:
                        w += 0.5
                    if any(c.isupper() for c in orig[1:]):
                        w += 0.5
                    total += w * 0.7  # dotted-partial is weaker than full exact
    return total


def _symbol_dunder_match(node: Node, symbols: Sequence[str], text_lower: str) -> float:
    """Match Django-style dunder lookups to node stems/symbols.

    E.g. issue mentions ``__isnull`` → node with symbol ``IsNull`` or
    file stem ``lookups`` scores here. This is the 'django dunder' signal.
    """
    if not text_lower:
        return 0.0
    # Extract dunder tokens: __isnull, __exact, __gte, email__isnull etc
    dunders: List[str] = []
    for m in re.finditer(r"__([a-z][a-z0-9_]{1,15})\b", text_lower):
        dunders.append(m.group(1))
    if not dunders:
        return 0.0
    hits = 0.0
    # Lookup-type files are the usual suspects
    if node.src_path and "lookups" in node.src_path.lower():
        hits += 1.0
    # Match symbol names directly (IsNull, Exact, GreaterThanOrEqual etc)
    sym_low = {s.lower() for s in symbols}
    for d in dunders:
        if d in sym_low:
            hits += 1.0
        # camelcase form: isnull → IsNull
        pascal = "".join(p.capitalize() for p in d.split("_"))
        if pascal.lower() in sym_low:
            hits += 1.0
    return hits


# ---------------------------------------------------------------------------
# v2.3 signals: domain bias, error_code, upper_const, mgmt_command
# ---------------------------------------------------------------------------
# Vocabulary that signals the issue is about ORM / db.models domain
_ORM_VOCAB = (
    "queryset", "queryset.", "objects.", "models.model", "meta.ordering",
    "primary_key", "foreignkey", "manytomany", "onetoone", "db_table",
    "db_index", "makemigrations", "migration", "to_field", "on_delete",
    "select_related", "prefetch_related", "annotate", "aggregate",
    "db.models", "models.", "queries", "sql", "schema_editor",
)
# Vocabulary that signals the issue is about HTML forms / form fields
_FORM_VOCAB = (
    "forms.form", "modelform", "form field", "widget", "render_value",
    "cleaned_data", "is_valid()", "request.post", "<input", "<form",
    "<select", "render(", "html=", "errorlist", "boundfield",
    "formset", "helptext", "help_text",
)
# Vocabulary that signals admin / contrib.admin domain
_ADMIN_VOCAB = (
    "admin.", "adminsite", "modeladmin", "list_display", "list_filter",
    "admin/", "admin panel",
)


def _domain_bias(node: Node, q_lower: str) -> float:
    """Disambiguate Django sub-domains based on issue vocabulary.

    Empirical: 5/9 close failures in v2.2 were `db/models/fields/__init__.py`
    losing to `forms/fields.py` by a tiny PageRank tiebreaker. The two share
    the basename `fields.py` but live in clearly different domains. When the
    issue text uses ORM-specific vocabulary, boost db/models/* and demote
    forms/*; when it uses form-specific vocabulary, do the reverse.

    Returns a signed score in roughly [-1, +1]; multiplied by ``beta_domain``.
    """
    sp = (node.src_path or "").replace("\\", "/").lower()
    if not sp:
        return 0.0

    orm_score = sum(1 for v in _ORM_VOCAB if v in q_lower)
    form_score = sum(1 for v in _FORM_VOCAB if v in q_lower)
    admin_score = sum(1 for v in _ADMIN_VOCAB if v in q_lower)

    # Net intent: positive => ORM, negative => forms
    net_orm_form = orm_score - form_score
    if abs(net_orm_form) < 1:
        net_orm_form = 0
    bias = math.tanh(net_orm_form / 3.0)  # in (-1, 1), saturating

    in_db_models = "/db/models/" in ("/" + sp) or sp.startswith("db/models/") or "/django/db/models/" in ("/" + sp)
    in_db_general = "/db/" in ("/" + sp) or sp.startswith("db/")
    in_forms = "/forms/" in ("/" + sp) or sp.startswith("forms/") or "/django/forms/" in ("/" + sp)
    in_admin = "/contrib/admin/" in ("/" + sp) or sp.startswith("contrib/admin/")

    s = 0.0
    if in_db_models:
        s += 1.0 * bias       # ORM context boosts db/models, forms context demotes
    elif in_db_general:
        s += 0.5 * bias
    if in_forms:
        s += -1.0 * bias      # opposite sign
    if in_admin and admin_score >= 1:
        s += 0.5
    return s


_ERROR_CODE_RE = re.compile(r"\b([a-z][a-z_]{1,15})\.([EW]\d{3,4})\b", re.IGNORECASE)


def _extract_error_codes(text: str) -> List[Tuple[str, str]]:
    """Extract Django check codes like `models.E028`, `fields.E001`.

    Returns a list of (app_prefix, code) tuples in lowercase.
    """
    out: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for m in _ERROR_CODE_RE.finditer(text):
        prefix = m.group(1).lower()
        code = m.group(2).upper()
        key = (prefix, code)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _error_code_match(node: Node, error_codes: Sequence[Tuple[str, str]]) -> float:
    """Boost nodes that are likely homes for the cited check codes.

    `models.E028` -> `core/checks/model_checks.py` or `db/models/*checks*.py`.
    `fields.E001` -> `db/models/fields/__init__.py` (field validation).
    `auth.E003`   -> `contrib/auth/checks.py`.
    `admin.E108`  -> `contrib/admin/checks.py`.
    """
    if not error_codes or not node.src_path:
        return 0.0
    sp = node.src_path.replace("\\", "/").lower()
    score = 0.0
    for prefix, _code in error_codes:
        # checks files for the prefix
        if f"checks/{prefix}" in sp or f"{prefix}_checks" in sp:
            score += 1.0
        elif sp.endswith(f"{prefix}/checks.py") or f"/{prefix}/checks.py" in sp:
            score += 1.0
        elif "/checks/" in sp and prefix in sp:
            score += 0.7
        # field validation codes -> field __init__
        elif prefix == "fields" and "db/models/fields/__init__" in sp:
            score += 0.6
        elif prefix == "models" and "db/models/" in sp and "checks" in sp:
            score += 0.8
    return score


_UPPER_CONST_RE = re.compile(r"\b[A-Z][A-Z0-9_]{4,}\b")
_UPPER_CONST_STOP = {"HTTP", "HTML", "JSON", "YAML", "DEBUG", "INFO", "WARN", "ERROR",
                     "FALSE", "TRUE", "NONE", "NULL", "GET", "POST", "PUT", "DELETE",
                     "HEAD", "OPTIONS", "PATCH", "BEGIN", "COMMIT", "ROLLBACK",
                     "SELECT", "INSERT", "UPDATE", "WHERE", "ORDER", "GROUP",
                     "SQLITE", "MYSQL", "POSTGRES", "ASCII", "UTF"}


def _extract_upper_constants(text: str) -> List[str]:
    """Extract Django settings-style constants: ``FILE_UPLOAD_PERMISSIONS``,
    ``LANGUAGE_CODE``, etc. Filters out HTTP verbs, log levels, SQL keywords.
    """
    out: List[str] = []
    seen: Set[str] = set()
    for m in _UPPER_CONST_RE.findall(text):
        if m in _UPPER_CONST_STOP or m in seen:
            continue
        # require at least one underscore (multi-word) OR length >= 8 to
        # filter out random acronyms like CSRF, MIDDLEWARE alone.
        if "_" not in m and len(m) < 8:
            continue
        seen.add(m)
        out.append(m)
    return out


def _upper_const_match(node: Node, upper_consts: Sequence[str]) -> float:
    """Settings constants live in conf/global_settings.py or conf/__init__.py.

    If 1+ Django-style UPPER_CONSTANTS appear in the issue, strongly boost
    these settings homes. Also lightly boost any file that contains the
    constant in its content (via symbols/path tokens).
    """
    if not upper_consts or not node.src_path:
        return 0.0
    sp = node.src_path.replace("\\", "/").lower()
    score = 0.0
    if "conf/global_settings" in sp or sp.endswith("conf/__init__.py") or "/conf/__init__" in sp:
        score += float(min(len(upper_consts), 3))
    elif "settings" in sp:
        score += 0.5
    return score


_MGMT_VERBS = (
    "sqlmigrate", "makemigrations", "migrate", "showmigrations", "squashmigrations",
    "loaddata", "dumpdata", "inspectdb", "runserver", "collectstatic",
    "createsuperuser", "changepassword", "shell", "dbshell", "flush",
    "startproject", "startapp", "check", "test", "compilemessages",
    "makemessages", "sendtestemail", "diffsettings", "sqlflush",
    "sqlsequencereset", "validate",
)


def _extract_mgmt_commands(text: str) -> List[str]:
    """Detect Django management command verbs cited in the issue."""
    out: List[str] = []
    seen: Set[str] = set()
    tl = text.lower()
    for verb in _MGMT_VERBS:
        # word-boundary-ish: surrounded by non-word OR ./manage.py prefix
        if re.search(r"(?:^|[\s`'\"./])" + re.escape(verb) + r"(?:[\s`'\":(]|$)", tl):
            if verb not in seen:
                seen.add(verb)
                out.append(verb)
    return out


def _mgmt_command_match(node: Node, mgmt_verbs: Sequence[str]) -> float:
    """`sqlmigrate` issue -> `core/management/commands/sqlmigrate.py`.

    Strongest deterministic signal in this category: management verb maps
    1:1 to a known file path inside Django.
    """
    if not mgmt_verbs or not node.src_path:
        return 0.0
    sp = node.src_path.replace("\\", "/").lower()
    score = 0.0
    for verb in mgmt_verbs:
        target = f"management/commands/{verb}.py"
        if sp.endswith(target) or f"/{target}" in sp:
            score += 1.0
        # Some verbs like `migrate`/`makemigrations`/`sqlmigrate` also touch
        # `db/migrations/executor.py` and `db/migrations/loader.py`
        elif verb in ("sqlmigrate", "migrate", "makemigrations", "showmigrations") and (
            sp.endswith("db/migrations/executor.py") or sp.endswith("db/migrations/loader.py")
        ):
            score += 0.4
    return score


# ---------------------------------------------------------------------------
# Public scoring entrypoint
# ---------------------------------------------------------------------------
def score_nodes_v2(
    doc: exvisitDoc,
    repo_root: Path,
    text: str,
    meta: Optional[GraphMeta],
    config: V2Config,
) -> List[ScoredNode]:
    """Return all nodes scored by v2; sorted desc by score (no zero filtering)."""
    nodes = doc.all_nodes()
    if not nodes:
        return []

    # Per-node symbol & meta lookup
    node_symbols: Dict[str, List[str]] = {}
    node_kind: Dict[str, str] = {}
    node_pr: Dict[str, float] = {}
    node_cluster: Dict[str, str] = {}
    cluster_size: Dict[str, int] = {}

    if meta is not None:
        for fqn, nm in meta.nodes.items():
            node_symbols[fqn] = nm.symbols
            node_kind[fqn] = nm.kind
            node_pr[fqn] = nm.pagerank
            node_cluster[fqn] = nm.cluster
        cluster_size = dict(meta.cluster_size)

    # Fill defaults for nodes missing meta (legacy `.exv` without sidecar)
    for n in nodes:
        if n.fqn not in node_kind:
            # cheap kind inference from src_path
            sp = (n.src_path or "").replace("\\", "/").lower()
            name = Path(sp).name
            if "/tests/" in sp or sp.startswith("tests/") or name.startswith("test_"):
                node_kind[n.fqn] = "test"
            elif name in ("__init__.py", "apps.py", "urls.py", "settings.py", "global_settings.py"):
                node_kind[n.fqn] = "registry"
            else:
                node_kind[n.fqn] = "normal"
            node_pr.setdefault(n.fqn, 0.0)
            node_cluster.setdefault(n.fqn, str(Path(sp).parent) if sp else "")
            node_symbols.setdefault(n.fqn, [])
    if not cluster_size:
        for c in node_cluster.values():
            cluster_size[c] = cluster_size.get(c, 0) + 1

    # Tokenize text once
    q_lower = text.lower()
    q_tokens = _tokenize_text(text)
    code_terms = _extract_code_terms(text)
    frames = _extract_frames(text)
    explicit_paths = _extract_explicit_paths(text)
    error_codes = _extract_error_codes(text)
    upper_consts = _extract_upper_constants(text)
    mgmt_verbs = _extract_mgmt_commands(text)

    # Build BM25 corpus
    corpus_bags: List[List[str]] = []
    bag_index: Dict[str, List[str]] = {}
    for n in nodes:
        bag = _node_token_bag(n, node_symbols.get(n.fqn, []))
        bag_index[n.fqn] = bag
        corpus_bags.append(bag)
    idf, avg_dl = _build_idf(corpus_bags)

    # Frame → node lookup (does a trace point inside this node?)
    frames_by_node: Dict[str, bool] = {}
    if frames:
        for n in nodes:
            sp = (n.src_path or "").replace("\\", "/").lower()
            if not sp:
                continue
            for f in frames:
                fp = f.path.lower()
                if fp.endswith(sp) or sp.endswith(fp) or Path(fp).name == Path(sp).name:
                    frames_by_node[n.fqn] = True
                    break

    B = config.betas
    out: List[ScoredNode] = []
    for n in nodes:
        bag = bag_index[n.fqn]
        kind = node_kind[n.fqn]

        s_lex = _bm25_score(q_tokens, bag, idf, avg_dl)
        s_trace = _trace_overlap(n, frames)
        s_sym = _max_symbol_overlap(code_terms, node_symbols.get(n.fqn, []))
        s_cent = math.log1p(node_pr.get(n.fqn, 0.0) * 1000.0)  # rescale for dynamic range
        cs = max(1, cluster_size.get(node_cluster.get(n.fqn, ""), 1))
        s_idf = math.log(1.0 + 1.0 / cs)
        s_reg = 1.0 if kind == "registry" else 0.0
        s_stem = _stem_in_text(n, q_lower)
        s_path = _path_term_match(n, code_terms)
        s_explicit = _explicit_path_match(n, explicit_paths)
        s_sym_exact = _symbol_exact_match(n, node_symbols.get(n.fqn, []), code_terms)
        s_dunder = _symbol_dunder_match(n, node_symbols.get(n.fqn, []), q_lower)
        s_domain = _domain_bias(n, q_lower)
        s_error_code = _error_code_match(n, error_codes)
        s_upper_const = _upper_const_match(n, upper_consts)
        s_mgmt = _mgmt_command_match(n, mgmt_verbs)
        s_test_pen = _test_gate(
            n, kind, q_lower, config.test_admit_terms,
            trace_into_node=frames_by_node.get(n.fqn, False),
        )
        s_migration_pen = _migration_gate(
            n, kind, q_lower, config.migration_admit_terms,
            trace_into_node=frames_by_node.get(n.fqn, False),
        )

        score = (
            B.get("lex", 1.0) * s_lex
            + B.get("trace", 2.5) * s_trace
            + B.get("sym", 1.4) * s_sym
            + B.get("cent", 1.2) * s_cent
            + B.get("idf", 0.8) * s_idf
            + B.get("reg", 0.9) * s_reg
            + B.get("stem", 0.7) * s_stem
            + B.get("path", 0.9) * s_path
            + B.get("explicit_path", 6.0) * s_explicit
            + B.get("symbol_exact", 5.0) * s_sym_exact
            + B.get("dunder", 8.0) * s_dunder
            + B.get("domain", 0.0) * s_domain
            + B.get("error_code", 0.0) * s_error_code
            + B.get("upper_const", 0.0) * s_upper_const
            + B.get("mgmt_command", 0.0) * s_mgmt
        )
        if s_test_pen > 0:
            # Multiplicative gate: when a node is a test file *and* the issue
            # does not request test infra, attenuate strongly. Subtraction is
            # unreliable because BM25 lex can dwarf the constant penalty.
            score = score * 0.25 - B.get("test", 2.0)
        if s_migration_pen > 0:
            # Migration gate: similar rationale — registry/always-include
            # flooded top-K with migrations that are almost never the oracle.
            score = score * 0.15 - B.get("migration", 3.0)

        components = {
            "lex": s_lex, "trace": s_trace, "sym": s_sym, "cent": s_cent,
            "idf": s_idf, "reg": s_reg, "stem": s_stem, "path": s_path,
            "explicit_path": s_explicit, "symbol_exact": s_sym_exact,
            "dunder": s_dunder, "domain": s_domain, "error_code": s_error_code,
            "upper_const": s_upper_const, "mgmt_command": s_mgmt,
            "test_pen": s_test_pen, "migration_pen": s_migration_pen,
        }
        reasons: List[str] = []
        if s_explicit > 0:
            reasons.append(f"explicit-path={s_explicit:.1f}")
        if s_sym_exact > 0:
            reasons.append(f"symbol-exact={s_sym_exact:.1f}")
        if s_dunder > 0:
            reasons.append(f"dunder={s_dunder:.1f}")
        if s_domain != 0.0:
            reasons.append(f"domain={s_domain:+.2f}")
        if s_error_code > 0:
            reasons.append(f"error-code={s_error_code:.1f}")
        if s_upper_const > 0:
            reasons.append(f"upper-const={s_upper_const:.1f}")
        if s_mgmt > 0:
            reasons.append(f"mgmt-cmd={s_mgmt:.1f}")
        if s_trace > 0:
            reasons.append("trace-overlap")
        if s_sym >= 0.85:
            reasons.append("symbol-match")
        if s_lex > 0:
            reasons.append("bm25-lex")
        if s_reg > 0:
            reasons.append("registry-prior")
        if s_cent > 0:
            reasons.append(f"pagerank={node_pr.get(n.fqn,0):.4f}")
        if s_idf > 0:
            reasons.append(f"cluster-idf(size={cs})")
        if s_test_pen > 0:
            reasons.append("test-penalty")
        if s_migration_pen > 0:
            reasons.append("migration-penalty")
        if s_stem > 0:
            reasons.append("stem-in-text")
        if s_path > 0:
            reasons.append("path-term")

        out.append(ScoredNode(score=score, node=n, components=components, reasons=reasons))

    out.sort(key=lambda x: (-x.score, x.node.fqn))
    return out


# ---------------------------------------------------------------------------
# Code-term extractor (lighter version of blast.extract_issue_terms)
# ---------------------------------------------------------------------------
_CODE_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|[A-Za-z0-9_./-]+\.py|`([^`\n]+)`")


def _extract_code_terms(text: str) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    # backticks
    for m in re.findall(r"`([^`\n]+)`", text):
        m = m.strip()
        if m and m.lower() not in seen:
            seen.add(m.lower())
            out.append(m)
    # dotted symbols + .py paths
    for m in re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|[A-Za-z0-9_./-]+\.py", text):
        if m.lower() not in seen:
            seen.add(m.lower())
            out.append(m)
    # CamelCase identifiers (likely class names)
    for m in re.findall(r"\b[A-Z][a-zA-Z0-9_]{2,}\b", text):
        if m.lower() not in seen:
            seen.add(m.lower())
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# Anchor selection — top-K + margin test
# ---------------------------------------------------------------------------
def select_anchors(scored: Sequence[ScoredNode], config: V2Config) -> Tuple[List[ScoredNode], float, bool]:
    """Return (anchors, confidence, low_margin).

    If `low_margin` is True, multiple anchors should be returned to the agent;
    otherwise a single anchor is high-confidence.
    """
    if not scored:
        return [], 0.0, False
    top = list(scored[: max(config.topk, 1)])
    # softmax-normalize for confidence reporting
    T = max(config.softmax_temp, 1e-3)
    raw = [n.score for n in scored[: config.topk * 2 or 6]]
    if not raw:
        return [scored[0]], 1.0, False
    m = max(raw)
    exps = [math.exp((s - m) / T) for s in raw]
    Z = sum(exps) or 1.0
    probs = [e / Z for e in exps]
    p1 = probs[0]
    p2 = probs[1] if len(probs) > 1 else 0.0
    confidence = round(p1, 3)
    margin = p1 - p2
    low_margin = margin < config.anchor_margin
    if not low_margin:
        return [top[0]], confidence, False
    # return up to topk while margin between consecutive is small
    out = [top[0]]
    for i in range(1, len(top)):
        if probs[i] >= p1 - config.anchor_margin * 1.5:
            out.append(top[i])
        else:
            break
    return out, confidence, True
