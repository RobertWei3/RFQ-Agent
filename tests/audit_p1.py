"""Independent audit of the Phase 1 synthetic dataset.

This script deliberately does NOT import anything from ``rfq_agent``. It is
written only against the dataset on disk (pages, internal DB, ground truth) and
RFQ-SPEC.md sections 6 and 9-11, so it can catch generator bugs that the
generator's own tests would share.

Checks:
  1. chain    - every supersession chain in ground truth can be fully traced
                from the interchange page through catalog / MSRP page text.
  2. msrp     - ground-truth MSRP (value, status, source OE) agrees with the
                MSRP pages, including the "walk back along the chain" fallback.
  3. weight   - external spec page weights, after unit conversion
                (kg / g / lb), agree with ground truth; conflicts are real.
  4. leakage  - ground truth cannot be inferred from part-number patterns
                (shared digits, rank correlations, chain direction, surface
                features predicting hard-case tags).

Usage:
    uv run python tests/audit_p1.py [--dataset DIR] [--gt FILE ...] [-v]

Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import re
import sqlite3
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = PROJECT_ROOT / "data" / "generated" / "n200_s42"

LB_TO_KG = 0.45359237
GT_WEIGHT_HALF_ULP = 0.0005  # ground-truth weights are stored with 3 decimals
SPEC_CONFLICT_MIN_REL_DIFF = 0.05

# Leakage thresholds. Sample sizes are small (~200 rows, ~40 chain edges), so
# these are set loose enough to avoid false alarms on honest random data while
# still catching systematic patterns.
MIN_SUPPORT_CORR = 20
MAX_ABS_SPEARMAN = 0.30
MIN_EDGES_DIRECTION = 10
DIRECTION_BAND = (0.20, 0.80)
MIN_SUPPORT_FEATURE = 10
MAX_FEATURE_PRECISION = 0.80
MAX_LABEL_BASE_RATE = 0.50
FEATURE_ALPHA = 0.01  # family-wise, Bonferroni-corrected over all feature x label cells
SHUFFLE_ROUNDS = 200

MATERIAL_SYNONYMS = {
    "aluminum": "aluminum",
    "aluminium": "aluminum",
    "die-cast aluminum": "aluminum",
    "al alloy (adc12)": "aluminum",
    "cast iron": "cast_iron",
    "grey cast iron": "cast_iron",
    "gray cast iron": "cast_iron",
    "gg25 iron": "cast_iron",
    "brass": "brass",
    "yellow brass": "brass",
    "cuzn brass": "brass",
    "stainless steel": "stainless_steel",
    "stainless steel (304)": "stainless_steel",
    "ss304": "stainless_steel",
}

# Part-number token: starts and ends with an alphanumeric, may contain . and -.
PN = r"([0-9A-Za-z][0-9A-Za-z.\-]*[0-9A-Za-z])"
RE_SUPERSEDED_BY = re.compile(rf"superseded\s+by:?\s+{PN}", re.I)
RE_REPLACES = re.compile(rf"\breplaces(?:\s+old\s+number)?:?\s+{PN}", re.I)
RE_REDIRECT = re.compile(rf"part\s+{PN}\s+is\s+no\s+longer\s+sold.*?order\s+{PN}", re.I)
RE_IC_CANDIDATE = re.compile(rf"^-\s+.*?\s{PN}\s+\(")
RE_IC_NOT_INTERCHANGEABLE = re.compile(r"not\s+interchangeable", re.I)
RE_PRICE = re.compile(
    r"(?:MSRP\)?|suggested retail price)\s*:?\s*(USD|\$)\s*([0-9][0-9,]*\.?[0-9]*)", re.I
)
RE_DISCONTINUED = re.compile(r"discontinued", re.I)
RE_WEIGHT = re.compile(r"weight[^0-9\n]*?([0-9]+(?:\.[0-9]+)?)\s*(kg|g|lb)\b", re.I)
RE_MATERIAL = re.compile(r"^(?:body|housing material|material)\s*:\s*(.+)$", re.I | re.M)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def norm(pn: str) -> str:
    """Independent part-number normalisation: NFKC, uppercase, alphanumerics only."""
    return re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKC", pn).upper())


def digits(pn: str) -> str:
    return re.sub(r"\D", "", pn)


def shape(pn: str) -> str:
    """Format signature of a part number, e.g. '4H3-250-125B' -> 'ANA-NNN-NNNA'."""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "N", pn))


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_pages(dataset: Path, name: str) -> dict[str, dict]:
    """Index a page corpus by normalised key; a key may map to several pages."""
    index: dict[str, list[dict]] = defaultdict(list)
    for page in load_jsonl(dataset / "pages" / f"{name}.jsonl"):
        for key in page["keys"]:
            index[norm(key)].append(page)
    return index


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return 0.0 if vx == 0 or vy == 0 else cov / (vx * vy) ** 0.5


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def shares_digit_run(a: str, b: str, length: int = 4) -> bool:
    da, db = digits(a), digits(b)
    return any(da[i : i + length] in db for i in range(len(da) - length + 1))


def common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


@dataclass
class CheckResult:
    name: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: int = 0

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    @property
    def ok(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------


@dataclass
class Corpus:
    catalog: dict[str, list[dict]]
    interchange: dict[str, list[dict]]
    msrp: dict[str, list[dict]]
    spec: dict[str, list[dict]]
    # Supersession graph over normalised OE numbers, built from page text only.
    successors: dict[str, set[str]]
    predecessors: dict[str, set[str]]
    edge_sources: dict[tuple[str, str], list[str]]


def build_corpus(dataset: Path) -> Corpus:
    catalog = load_pages(dataset, "oem_catalog")
    interchange = load_pages(dataset, "interchange")
    msrp = load_pages(dataset, "msrp")
    spec = load_pages(dataset, "external_spec")

    successors: dict[str, set[str]] = defaultdict(set)
    predecessors: dict[str, set[str]] = defaultdict(set)
    edge_sources: dict[tuple[str, str], list[str]] = defaultdict(list)

    def add_edge(old: str, new: str, page_id: str) -> None:
        old, new = norm(old), norm(new)
        if old == new:
            return
        successors[old].add(new)
        predecessors[new].add(old)
        edge_sources[(old, new)].append(page_id)

    for key, pages in catalog.items():
        for page in pages:
            for m in RE_SUPERSEDED_BY.finditer(page["text"]):
                add_edge(key, m.group(1), page["page_id"])
            for m in RE_REPLACES.finditer(page["text"]):
                add_edge(m.group(1), key, page["page_id"])
    for pages in msrp.values():
        for page in pages:
            for m in RE_REDIRECT.finditer(page["text"]):
                add_edge(m.group(1), m.group(2), page["page_id"])

    return Corpus(catalog, interchange, msrp, spec, successors, predecessors, edge_sources)


def interchange_candidates(corpus: Corpus, customer_part_no: str) -> list[str] | None:
    """Candidate OE numbers listed on the interchange page, or None if there is no page."""
    pages = corpus.interchange.get(norm(customer_part_no))
    if not pages:
        return None
    out: list[str] = []
    for page in pages:
        for line in page["text"].splitlines():
            if RE_IC_NOT_INTERCHANGEABLE.search(line):
                continue
            m = RE_IC_CANDIDATE.match(line.strip())
            if m:
                out.append(m.group(1))
    return out


@dataclass
class MsrpPage:
    price: float | None
    currency: str | None
    discontinued: bool
    redirect_to: str | None


def parse_msrp(corpus: Corpus, oe: str) -> list[MsrpPage]:
    out = []
    for page in corpus.msrp.get(norm(oe), []):
        text = page["text"]
        m = RE_PRICE.search(text)
        redirect = RE_REDIRECT.search(text)
        out.append(
            MsrpPage(
                price=float(m.group(2).replace(",", "")) if m else None,
                currency="USD" if m else None,
                discontinued=bool(RE_DISCONTINUED.search(text)),
                redirect_to=norm(redirect.group(2)) if redirect else None,
            )
        )
    return out


@dataclass
class SpecPage:
    page_id: str
    weight_raw: tuple[float, str] | None
    weight_kg: float | None
    weight_tol: float  # display rounding expressed in kg
    material_raw: str | None
    material: str | None


def to_kg(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit == "kg":
        return value
    if unit == "g":
        return value / 1000
    if unit == "lb":
        return value * LB_TO_KG
    raise ValueError(unit)


def parse_spec(page: dict) -> SpecPage:
    text = page["text"]
    weight_raw = weight_kg = None
    tol = 0.0
    m = RE_WEIGHT.search(text)
    if m:
        literal, unit = m.group(1), m.group(2).lower()
        value = float(literal)
        decimals = len(literal.split(".")[1]) if "." in literal else 0
        weight_raw = (value, unit)
        weight_kg = to_kg(value, unit)
        tol = to_kg(0.5 * 10**-decimals, unit)
    mat_raw = mat = None
    mm = RE_MATERIAL.search(text)
    if mm:
        mat_raw = mm.group(1).strip()
        mat = MATERIAL_SYNONYMS.get(mat_raw.lower())
    return SpecPage(page["page_id"], weight_raw, weight_kg, tol, mat_raw, mat)


# ---------------------------------------------------------------------------
# Check 1: supersession chains are traceable from page text
# ---------------------------------------------------------------------------


def trace_chain(corpus: Corpus, start: str) -> tuple[list[str], str | None]:
    """Walk back to the head and forward to the tail from ``start``.

    Returns (chain of normalised OEs head..tail, error or None).
    """
    seen = {start}
    head = start
    while corpus.predecessors.get(head):
        preds = corpus.predecessors[head]
        if len(preds) > 1:
            return [], f"{head} has multiple predecessors {sorted(preds)}"
        (head,) = preds
        if head in seen:
            return [], f"cycle through {head}"
        seen.add(head)
    chain = [head]
    node = head
    visited = {head}
    while corpus.successors.get(node):
        succs = corpus.successors[node]
        if len(succs) > 1:
            return [], f"{node} has multiple successors {sorted(succs)}"
        (node,) = succs
        if node in visited:
            return [], f"cycle through {node}"
        visited.add(node)
        chain.append(node)
    return chain, None


def check_chains(corpus: Corpus, rows: list[dict]) -> CheckResult:
    res = CheckResult("chain: supersession chains traceable from pages")

    # Global graph sanity: no node with >1 successor/predecessor, no cycles.
    for node, succs in corpus.successors.items():
        if len(succs) > 1:
            res.fail(f"graph: {node} superseded by several numbers {sorted(succs)}")
    for node in list(corpus.successors):
        _, err = trace_chain(corpus, node)
        if err and "cycle" in err:
            res.fail(f"graph: {err}")

    positions: Counter[str] = Counter()
    for row in rows:
        res.checked += 1
        rid, cpn = row["row_id"], row["customer_part_no"]
        status = row["oe_status_expected"]
        gt_chain = [norm(o) for o in row["supersession_chain"]]
        accepted = {norm(o) for o in row["accepted_oes"]}
        cands = interchange_candidates(corpus, cpn)

        if status == "not_found":
            if cands:
                res.fail(f"row {rid} {cpn}: not_found but interchange lists {cands}")
            if gt_chain or accepted or row["primary_oe"]:
                res.fail(f"row {rid} {cpn}: not_found but GT has OE data")
            continue

        if cands is None:
            res.fail(f"row {rid} {cpn}: no interchange page for a {status} row")
            continue
        if not cands:
            res.fail(f"row {rid} {cpn}: interchange page lists no candidates")
            continue

        tails: dict[str, list[str]] = {}
        for cand in cands:
            chain, err = trace_chain(corpus, norm(cand))
            if err:
                res.fail(f"row {rid} {cpn}: tracing {cand}: {err}")
                continue
            tails[chain[-1]] = chain

        if set(tails) != accepted:
            res.fail(
                f"row {rid} {cpn}: traced current OEs {sorted(tails)} != GT accepted {sorted(accepted)}"
            )
        primary = norm(row["primary_oe"] or "")
        if primary not in accepted:
            res.fail(f"row {rid} {cpn}: primary_oe {primary} not in accepted_oes")
        if gt_chain and gt_chain[-1] != primary:
            res.fail(f"row {rid} {cpn}: GT chain tail {gt_chain[-1]} != primary_oe {primary}")

        traced = tails.get(primary)
        if traced is not None and traced != gt_chain:
            res.fail(f"row {rid} {cpn}: traced chain {traced} != GT chain {gt_chain}")

        # Every link must be stated on at least one page, and every member must
        # have its own catalog page so a lookup on any number works.
        for old, new in itertools.pairwise(gt_chain):
            if (old, new) not in corpus.edge_sources:
                res.fail(f"row {rid} {cpn}: link {old} -> {new} not stated on any page")
        for oe in gt_chain:
            if oe not in corpus.catalog:
                res.fail(f"row {rid} {cpn}: chain member {oe} has no catalog page")

        is_chain = len(gt_chain) > 1
        if is_chain != ("supersession" in row["tags"]):
            res.fail(f"row {rid} {cpn}: chain length {len(gt_chain)} vs supersession tag mismatch")
        if status == "multiple" and len(accepted) < 2:
            res.fail(f"row {rid} {cpn}: status multiple with {len(accepted)} accepted OE")
        if status == "resolved" and len(accepted) != 1:
            res.fail(f"row {rid} {cpn}: status resolved with {len(accepted)} accepted OEs")

        if is_chain:
            for cand in cands:
                c = norm(cand)
                if c in gt_chain:
                    idx = gt_chain.index(c)
                    positions[
                        "head" if idx == 0 else "tail" if idx == len(gt_chain) - 1 else "middle"
                    ] += 1

    res.notes.append(f"interchange entry point within chains: {dict(positions)}")
    if positions and set(positions) == {"tail"}:
        res.fail("interchange always lists the chain tail; chain following is never exercised")
    return res


# ---------------------------------------------------------------------------
# Check 2: MSRP agrees with pages
# ---------------------------------------------------------------------------


def check_msrp(corpus: Corpus, rows: list[dict]) -> CheckResult:
    res = CheckResult("msrp: GT MSRP matches price pages")
    fallback_rows = 0
    for row in rows:
        res.checked += 1
        rid, cpn = row["row_id"], row["customer_part_no"]
        gt = row["msrp"]
        chain = [norm(o) for o in row["supersession_chain"]]

        # Expected source: walk back from the current OE, take the first number
        # with a priced page.
        expected_src = expected_page = None
        for oe in reversed(chain):
            priced = [p for p in parse_msrp(corpus, oe) if p.price is not None]
            if len(priced) > 1 and len({p.price for p in priced}) > 1:
                res.fail(f"row {rid} {cpn}: {oe} has conflicting MSRP pages")
            if priced:
                expected_src, expected_page = oe, priced[0]
                break

        # Redirect pages must agree with the chain direction.
        for oe in chain:
            for p in parse_msrp(corpus, oe):
                if p.redirect_to and (
                    oe == chain[-1] or chain[chain.index(oe) + 1] != p.redirect_to
                ):
                    res.fail(f"row {rid} {cpn}: MSRP redirect {oe} -> {p.redirect_to} off-chain")

        if expected_page is None:
            if gt["status"] != "not_found" or gt["value"] is not None or gt["source_oe"]:
                res.fail(f"row {rid} {cpn}: no priced page on chain {chain}, but GT msrp={gt}")
            continue

        src = norm(gt["source_oe"] or "")
        if src != expected_src:
            res.fail(f"row {rid} {cpn}: GT source_oe {src} != first priced OE walking back {expected_src}")
        if gt["value"] is None or abs(gt["value"] - expected_page.price) > 0.005:
            res.fail(f"row {rid} {cpn}: GT MSRP {gt['value']} != page {expected_page.price}")
        if expected_page.currency != "USD":
            res.fail(f"row {rid} {cpn}: page currency {expected_page.currency}")
        want_status = "discontinued" if expected_page.discontinued else "found"
        if gt["status"] != want_status:
            res.fail(f"row {rid} {cpn}: GT status {gt['status']} but page implies {want_status}")

        is_fallback = expected_src != chain[-1]
        fallback_rows += is_fallback
        if is_fallback != ("msrp_fallback" in row["tags"]):
            res.fail(f"row {rid} {cpn}: fallback={is_fallback} vs msrp_fallback tag mismatch")

    res.notes.append(f"rows priced via chain fallback: {fallback_rows}")
    return res


# ---------------------------------------------------------------------------
# Check 3: weight unit conversion (and material) vs external spec pages
# ---------------------------------------------------------------------------


def check_weight(corpus: Corpus, rows: list[dict], db_path: Path) -> CheckResult:
    res = CheckResult("weight: spec-page weights convert to GT kg")
    units: Counter[str] = Counter()

    db_parts: dict[str, tuple[str | None, float | None]] = {}
    if db_path.exists():
        with sqlite3.connect(db_path) as con:
            for pno, mat, wt in con.execute("SELECT our_part_no, material, weight_kg FROM our_parts"):
                db_parts[pno] = (mat, wt)
    else:
        res.notes.append(f"{db_path} missing; internal DB cross-check skipped")

    for row in rows:
        res.checked += 1
        rid, cpn = row["row_id"], row["customer_part_no"]
        truth, internal = row["spec"], row["internal_spec"]
        conflict = "spec_conflict" in row["tags"]

        if db_parts:
            db = db_parts.get(row["our_part_no"])
            if db is None:
                res.fail(f"row {rid} {cpn}: our_part_no {row['our_part_no']} not in internal DB")
            elif db != (internal["material"], internal["weight_kg"]):
                res.fail(f"row {rid} {cpn}: DB spec {db} != GT internal_spec {internal}")

        # Internal values, where present, are the ground truth.
        for key in ("material", "weight_kg"):
            if internal[key] is not None and internal[key] != truth[key]:
                res.fail(f"row {rid} {cpn}: internal {key}={internal[key]} != GT spec {truth[key]}")
        missing = [k for k in ("material", "weight_kg") if internal[k] is None]
        if bool(missing) != ("internal_spec_missing" in row["tags"]):
            res.fail(f"row {rid} {cpn}: internal missing {missing} vs internal_spec_missing tag")

        oes = {norm(o) for o in row["supersession_chain"] + row["accepted_oes"]}
        pages = [parse_spec(p) for oe in sorted(oes) for p in corpus.spec.get(oe, [])]
        if bool(pages) != bool(row["external_spec_available"]):
            res.fail(
                f"row {rid} {cpn}: {len(pages)} spec pages vs external_spec_available="
                f"{row['external_spec_available']}"
            )
        if missing and not pages:
            res.fail(f"row {rid} {cpn}: internal spec missing {missing} and no external page")
        if conflict and not pages:
            res.fail(f"row {rid} {cpn}: spec_conflict tag but no external page")

        for sp in pages:
            if sp.weight_kg is None:
                res.fail(f"row {rid} {cpn}: {sp.page_id} has no parsable weight")
                continue
            if sp.material is None:
                res.fail(f"row {rid} {cpn}: {sp.page_id} material {sp.material_raw!r} unrecognised")
            units[sp.weight_raw[1]] += 1
            tol = sp.weight_tol + GT_WEIGHT_HALF_ULP + 1e-9
            weight_ok = abs(sp.weight_kg - truth["weight_kg"]) <= tol
            material_ok = sp.material == truth["material"]

            if conflict:
                # The page must actually disagree with the internal (true) data.
                rel = abs(sp.weight_kg - truth["weight_kg"]) / truth["weight_kg"]
                if rel < SPEC_CONFLICT_MIN_REL_DIFF and material_ok:
                    res.fail(
                        f"row {rid} {cpn}: spec_conflict but {sp.page_id} agrees with GT "
                        f"({sp.weight_raw} ~ {truth['weight_kg']} kg, {sp.material})"
                    )
                continue

            if not weight_ok:
                hint = ""
                value, unit = sp.weight_raw
                for alt in ("kg", "g", "lb"):
                    if alt != unit and abs(to_kg(value, alt) - truth["weight_kg"]) <= 0.002:
                        hint = f" (matches if read as {alt}: unit bug)"
                res.fail(
                    f"row {rid} {cpn}: {sp.page_id} says {value} {unit} = {sp.weight_kg:.4f} kg, "
                    f"GT {truth['weight_kg']} kg{hint}"
                )
            if not material_ok:
                res.fail(f"row {rid} {cpn}: {sp.page_id} material {sp.material} != GT {truth['material']}")

    res.notes.append(f"weight units on pages: {dict(units)}")
    if len(units) < 3:
        res.fail(f"expected kg, g and lb all exercised, got {sorted(units)}")
    return res


# ---------------------------------------------------------------------------
# Check 4: ground truth is not derivable from part-number patterns
# ---------------------------------------------------------------------------


def shuffled_rate(pairs_a: list[str], pairs_b: list[str], rng: random.Random) -> float:
    rates = []
    b = list(pairs_b)
    for _ in range(SHUFFLE_ROUNDS):
        rng.shuffle(b)
        rates.append(statistics.fmean(shares_digit_run(x, y) for x, y in zip(pairs_a, b)))
    return statistics.fmean(rates)


def check_leakage(rows: list[dict], all_rows: list[dict]) -> CheckResult:
    res = CheckResult("leakage: GT not inferable from part-number patterns")
    rng = random.Random(0)

    # (a) Shared 4-digit runs between customer part and answers vs. a shuffled baseline.
    targets = {
        "our_part_no": lambda r: r["our_part_no"],
        "primary_oe": lambda r: r["primary_oe"],
        "chain_head": lambda r: r["supersession_chain"][0] if r["supersession_chain"] else None,
    }
    for label, get in targets.items():
        pairs = [(r["customer_part_no"], get(r)) for r in rows if get(r)]
        if len(pairs) < MIN_SUPPORT_CORR:
            continue
        a, b = zip(*pairs)
        real = statistics.fmean(shares_digit_run(x, y) for x, y in pairs)
        base = shuffled_rate(list(a), list(b), rng)
        res.notes.append(f"shared 4-digit run customer~{label}: {real:.3f} (shuffled {base:.3f})")
        if real > 2 * base + 0.05:
            res.fail(f"customer part shares digits with {label}: {real:.2f} vs baseline {base:.2f}")

    # (b) Rank correlations between customer-number digits and answers, per category.
    def num(pn: str | None) -> int | None:
        d = digits(pn or "")
        return int(d) if d else None

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["spec"]["category"]].append(r)
    answer_fns = {
        "our_part_no digits": lambda r: num(r["our_part_no"]),
        "primary_oe digits": lambda r: num(r["primary_oe"]),
        "msrp": lambda r: r["msrp"]["value"],
        "weight_kg": lambda r: r["spec"]["weight_kg"],
        "cycle_time": lambda r: r["spec"]["cycle_time_min"],
    }
    features = {
        "customer digits": lambda r: num(r["customer_part_no"]),
        "our_part digits": lambda r: num(r["our_part_no"]),
    }
    for cat, cat_rows in sorted(by_cat.items()):
        for fname, ffn in features.items():
            for aname, afn in answer_fns.items():
                if fname.split()[0] in aname.split()[0]:
                    continue
                pts = [(ffn(r), afn(r)) for r in cat_rows if ffn(r) is not None and afn(r) is not None]
                if len(pts) < MIN_SUPPORT_CORR:
                    continue
                rho = spearman([p[0] for p in pts], [p[1] for p in pts])
                if abs(rho) > MAX_ABS_SPEARMAN:
                    res.fail(f"[{cat}] spearman({fname}, {aname}) = {rho:+.2f} over {len(pts)} rows")

    # (c) Supersession direction must not follow a numeric / lexical order, and
    #     consecutive numbers must not be suspiciously similar.
    edges = []
    for r in all_rows:
        edges += list(itertools.pairwise(r["supersession_chain"]))
    edges = list(dict.fromkeys(edges))
    if len(edges) >= MIN_EDGES_DIRECTION:
        lo, hi = DIRECTION_BAND
        lex_up = statistics.fmean(norm(b) > norm(a) for a, b in edges)
        num_up = statistics.fmean(int(digits(b) or 0) > int(digits(a) or 0) for a, b in edges)
        res.notes.append(f"chain edges={len(edges)}: new>old lexically {lex_up:.2f}, numerically {num_up:.2f}")
        for name, frac in (("lexical", lex_up), ("numeric", num_up)):
            if not lo <= frac <= hi:
                res.fail(f"supersession direction follows {name} order in {frac:.0%} of edges")

        same_shape = defaultdict(list)
        for r in all_rows:
            for oe in set(r["supersession_chain"] + r["accepted_oes"]):
                same_shape[shape(oe)].append(norm(oe))
        edge_pref = statistics.fmean(common_prefix_len(norm(a), norm(b)) for a, b in edges)
        rand_pref = []
        for a, _ in edges:
            pool = same_shape[shape(a)]
            if len(pool) > 1:
                rand_pref.append(common_prefix_len(norm(a), rng.choice(pool)))
        base = statistics.fmean(rand_pref) if rand_pref else 0.0
        res.notes.append(f"chain edge common-prefix mean {edge_pref:.2f} (random same-format {base:.2f})")
        if edge_pref > base + 1.5:
            res.fail(f"consecutive chain numbers look alike: prefix {edge_pref:.2f} vs {base:.2f}")
        if sum(shape(a) != shape(b) for a, b in edges) == 0 and len(edges) >= MIN_EDGES_DIRECTION:
            res.notes.append("all chain links keep the same brand format (expected)")
    else:
        res.notes.append(f"only {len(edges)} chain edges; direction test skipped")

    # (d) Many-to-one siblings should not have look-alike customer numbers.
    by_id = {r["row_id"]: r for r in all_rows}
    sib_pairs = {
        tuple(sorted((r["row_id"], s)))
        for r in all_rows
        for s in r["shares_oe_with"]
        if s in by_id
    }
    if sib_pairs:
        hits = statistics.fmean(
            shares_digit_run(by_id[a]["customer_part_no"], by_id[b]["customer_part_no"])
            for a, b in sib_pairs
        )
        res.notes.append(f"many-to-one sibling pairs={len(sib_pairs)}, share 4-digit run: {hits:.2f}")
        if hits > 0.5:
            res.fail(f"many-to-one siblings share customer-number digits in {hits:.0%} of pairs")

    # (e) Surface features predicting hard-case labels.
    def cust_suffix(r):
        m = re.match(r"^[A-Z]+-\d+(?:-(.+))?$", r["customer_part_no"])
        return m.group(1) if m and m.group(1) else "<none>"

    surface = {
        "customer suffix": cust_suffix,
        "customer last digit": lambda r: digits(r["customer_part_no"])[-1:],
        "customer first digit": lambda r: digits(r["customer_part_no"])[:1],
        "customer length": lambda r: len(r["customer_part_no"]),
        "our_part last digit": lambda r: digits(r["our_part_no"])[-1:],
        "our_part first digit": lambda r: digits(r["our_part_no"])[:1],
        "primary_oe format": lambda r: shape(r["primary_oe"]) if r["primary_oe"] else "<none>",
    }
    labels: dict[str, callable] = {
        f"tag:{t}": (lambda t: lambda r: t in r["tags"])(t)
        for t in sorted({t for r in all_rows for t in r["tags"]})
    }
    labels.update(
        {
            f"oe_status={s}": (lambda s: lambda r: r["oe_status_expected"] == s)(s)
            for s in ("multiple", "not_found")
        }
    )
    labels.update(
        {
            f"msrp={s}": (lambda s: lambda r: r["msrp"]["status"] == s)(s)
            for s in ("discontinued", "not_found")
        }
    )
    labels.update(
        {
            f"reachability={s}": (lambda s: lambda r: r["our_part_reachability"] == s)(s)
            for s in sorted({r["our_part_reachability"] for r in all_rows})
        }
    )
    # A "not_found" row has no OE by construction, so the OE format trivially
    # predicts it; that is not leakage from the input.
    trivial = {("primary_oe format", "tag:oe_not_found"), ("primary_oe format", "oe_status=not_found")}
    trivial |= {("primary_oe format", "msrp=not_found")}

    # Collect every (feature value, label) cell first so the significance level
    # can be corrected for the number of comparisons; with ~1000 cells a few
    # 80%-pure cells are expected from honest randomness alone.
    cells = []
    for fname, ffn in surface.items():
        groups: dict[object, list[dict]] = defaultdict(list)
        for r in all_rows:
            groups[ffn(r)].append(r)
        for lname, lfn in labels.items():
            if (fname, lname) in trivial:
                continue
            base = statistics.fmean(lfn(r) for r in all_rows)
            if base > MAX_LABEL_BASE_RATE or base == 0:
                continue
            for value, grp in groups.items():
                if len(grp) < MIN_SUPPORT_FEATURE:
                    continue
                hits = sum(lfn(r) for r in grp)
                cells.append((fname, value, lname, hits, len(grp), base))
    alpha = FEATURE_ALPHA / max(len(cells), 1)
    worst = None
    for fname, value, lname, hits, n, base in cells:
        pval = binom_sf(hits, n, base)
        if worst is None or pval < worst[0]:
            worst = (pval, fname, value, lname, hits, n, base)
        if hits / n >= MAX_FEATURE_PRECISION and pval < alpha:
            res.fail(
                f"{fname}={value!r} predicts {lname}: {hits}/{n} rows "
                f"(base rate {base:.0%}, p={pval:.1e} < {alpha:.1e})"
            )
    if worst:
        pval, fname, value, lname, hits, n, base = worst
        res.notes.append(
            f"surface-feature cells tested={len(cells)}; strongest {fname}={value!r} -> {lname} "
            f"{hits}/{n} (base {base:.0%}, p={pval:.1e}, corrected alpha {alpha:.1e})"
        )
    res.checked = len(all_rows)
    return res


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def report(results: list[CheckResult], verbose: bool, max_lines: int = 15) -> bool:
    all_ok = True
    for r in results:
        all_ok &= r.ok
        status = "PASS" if r.ok else f"FAIL ({len(r.failures)})"
        print(f"[{status:>9}] {r.name}  (rows checked: {r.checked})")
        for note in r.notes:
            print(f"            note: {note}")
        shown = r.failures if verbose else r.failures[:max_lines]
        for f in shown:
            print(f"            - {f}")
        if len(shown) < len(r.failures):
            print(f"            ... {len(r.failures) - len(shown)} more (use -v)")
    return all_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--gt",
        type=Path,
        nargs="*",
        help="ground-truth files to audit (default: every *ground_truth.jsonl in --dataset)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    dataset: Path = args.dataset
    if not dataset.is_dir():
        print(f"dataset dir not found: {dataset} (run `rfq data generate` first)")
        return 2
    gt_files = args.gt or sorted(dataset.glob("*ground_truth.jsonl"))
    if not gt_files:
        print(f"no ground truth files in {dataset}")
        return 2

    corpus = build_corpus(dataset)
    print(
        f"corpus: catalog={len(corpus.catalog)} interchange={len(corpus.interchange)} "
        f"msrp={len(corpus.msrp)} spec={len(corpus.spec)} "
        f"supersession edges={len(corpus.edge_sources)}"
    )

    all_ok = True
    for gt_file in gt_files:
        rows = load_jsonl(gt_file)
        print(f"\n=== {gt_file.name}: {len(rows)} rows")
        results = [
            check_chains(corpus, rows),
            check_msrp(corpus, rows),
            check_weight(corpus, rows, dataset / "internal.db"),
            check_leakage(rows, rows),
        ]
        all_ok &= report(results, args.verbose)

    print("\nAUDIT", "PASSED" if all_ok else "FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
