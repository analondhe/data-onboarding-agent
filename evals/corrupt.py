"""Turn clean tables into messy eval cases with answer keys.

A case is one messy CSV for one target table plus ``answer_key.json`` describing the true
source-to-target mapping, the columns that must stay unmapped, and every injected defect.
The sampled clean rows are written next to it as ``clean.csv`` for cell-level comparison.

Usage:
  python -m evals.corrupt all --seed 42 --clean-dir data/clean --out evals/cases
  python -m evals.corrupt case --table customers --corruptions rename_columns,null_variants \
      --seed 7 --clean-dir data/clean --out evals/cases --case-id my_case
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from faker import Faker

from datagen.schemas import TABLES, column_names, key_column, load_schema

CORRUPTIONS = (
    "rename_columns",
    "cryptic_names",
    "mixed_date_formats",
    "type_noise",
    "duplicates",
    "null_variants",
    "value_drift",
    "merge_columns",
    "extra_columns",
    "drop_required",
)
# Corruptions that change column names, so mapping cannot rely on exact name matches.
NAMING_CORRUPTIONS = ("rename_columns", "cryptic_names")

# Application order. Row-level duplication runs first so later per-column counts cover the
# final rows; value corruptions claim their columns before structural ones; names change last.
APPLY_ORDER = (
    "duplicates",
    "null_variants",
    "type_noise",
    "value_drift",
    "mixed_date_formats",
    "merge_columns",
    "drop_required",
    "extra_columns",
    "cryptic_names",
    "rename_columns",
)


class DefectType(StrEnum):
    """Fixed defect vocabulary shared by answer keys and (later) the agent's defect reports."""

    DUPLICATE_ROWS = "duplicate_rows"
    NULL_VARIANTS = "null_variants"
    TYPE_NOISE = "type_noise"
    VALUE_DRIFT = "value_drift"
    MIXED_DATE_FORMATS = "mixed_date_formats"
    MERGED_COLUMN = "merged_column"
    MISSING_REQUIRED_COLUMN = "missing_required_column"


class InfeasibleCorruptionError(ValueError):
    """The table has no column left that this corruption can apply to."""


NULL_TOKENS = ("NULL", "null", "-", "n/a", "N/A", "")
JUNK_TOKENS = ("N/A", "unknown", "TBD", "#VALUE!")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_LONG = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
DATE_FORMATS: dict[str, Callable[[date], str]] = {
    "YYYY-MM-DD": lambda d: d.isoformat(),
    "MM/DD/YYYY": lambda d: f"{d.month:02d}/{d.day:02d}/{d.year:04d}",
    "DD Mon YYYY": lambda d: f"{d.day:02d} {MONTHS[d.month - 1]} {d.year:04d}",
    "YYYYMMDD": lambda d: f"{d.year:04d}{d.month:02d}{d.day:02d}",
    "Month DD, YYYY": lambda d: f"{MONTHS_LONG[d.month - 1]} {d.day:02d}, {d.year:04d}",
}
SECONDARY_DATE_FORMATS = ("DD Mon YYYY", "YYYYMMDD", "Month DD, YYYY")
TRUE_VARIANTS = ("Y", "yes", "1", "TRUE", "T")
FALSE_VARIANTS = ("N", "no", "0", "FALSE", "F")
SYNONYMS = {
    "free": ("free tier", "Free Plan", "basic"),
    "pro": ("professional", "Pro Plan", "pro-monthly"),
    "enterprise": ("ent", "Enterprise Plan", "enterprise tier"),
    "trialing": ("trial", "in trial", "Trialling"),
    "active": ("live", "current"),
    "past_due": ("past due", "overdue", "Past-Due"),
    "canceled": ("cancelled", "churned", "terminated"),
    "open": ("outstanding", "unpaid"),
    "paid": ("settled", "Paid in full"),
    "void": ("voided", "cancelled"),
    "uncollectible": ("bad debt", "written off"),
}
# Human-style alternative names per target column, used by rename_columns.
RENAMES = {
    "customer_id": ("Customer ID", "Cust #", "Client Id", "Account Number"),
    "company_name": ("Company", "Company Name", "Organization", "Account Name"),
    "contact_email": ("Email", "Contact Email", "Primary Email", "E-mail Address"),
    "country": ("Country", "Country Code", "Ctry", "Billing Country"),
    "signup_date": ("Sign Up Dt", "Signup Date", "Date Joined", "Account Created"),
    "plan": ("Plan", "Tier", "Plan Type", "Pricing Tier"),
    "is_active": ("Active?", "Is Active", "Active Flag", "Account Active"),
    "subscription_id": ("Subscription ID", "Sub #", "Subscription Ref", "Contract Id"),
    "status": ("Status", "State", "Current Status"),
    "started_at": ("Start Date", "Started", "Sub Start", "Begin Dt"),
    "canceled_at": ("Cancel Date", "Cancelled On", "End Date", "Churn Dt"),
    "monthly_amount_usd": ("MRR", "Monthly Amount", "Monthly Price ($)", "Amt / Month"),
    "seats": ("Seats", "Licenses", "Seat Count", "Users"),
    "invoice_id": ("Invoice ID", "Invoice #", "Inv No", "Bill Number"),
    "invoice_date": ("Invoice Date", "Billed On", "Inv Dt", "Issue Date"),
    "due_date": ("Due Date", "Payment Due", "Due Dt", "Due By"),
    "amount_usd": ("Amount", "Total ($)", "Invoice Amount", "Amt USD"),
    "paid_at": ("Paid Date", "Payment Date", "Paid On", "Settled Dt"),
}
# Word abbreviations used by cryptic_names, e.g. plan -> pln_cd, signup_date -> sgnp_dt.
ABBREVIATIONS = {
    "customer": "cust", "company": "co", "name": "nm", "contact": "cntct", "email": "eml",
    "country": "ctry", "signup": "sgnp", "date": "dt", "plan": "pln", "is": "", "active": "actv",
    "subscription": "sub", "status": "stat", "started": "strt", "at": "dt", "canceled": "cncl",
    "monthly": "mo", "amount": "amt", "usd": "usd", "seats": "sts", "invoice": "inv",
    "due": "due", "paid": "pd", "id": "id",
}  # fmt: skip
EXTRA_COLUMNS = (
    "legacy_notes",
    "import_batch",
    "sales_rep",
    "row_checksum",
    "region_code",
    "crm_lead_score",
    "last_modified_by",
)
# Column pairs a legacy export plausibly stores in one field, per table.
MERGE_PAIRS = {
    "customers": [
        ("company_name", "contact_email"),
        ("company_name", "country"),
        ("plan", "is_active"),
    ],
    "subscriptions": [
        ("plan", "status"),
        ("started_at", "canceled_at"),
        ("monthly_amount_usd", "seats"),
    ],
    "invoices": [
        ("invoice_date", "due_date"),
        ("amount_usd", "status"),
        ("status", "paid_at"),
    ],
}


@dataclass
class Column:
    name: str
    values: list[str]
    targets: list[str]  # target columns this source column carries; empty for extras
    separator: str | None = None  # set when the column is two targets merged
    claimed: bool = False  # already used by a value or structural corruption
    cryptic: bool = False  # renamed by cryptic_names; rename_columns leaves it alone

    @property
    def target(self) -> str:
        return self.targets[0]


def row_numbers(positions: list[int]) -> list[int]:
    """Convert 0-based positions in the messy file to 1-based row numbers (the ``_row`` value)."""
    return [p + 1 for p in positions]


@dataclass
class Defect:
    type: DefectType
    column: Column | None  # None when the source column was removed
    target_columns: list[str]
    affected_rows: int
    details: dict[str, Any] = field(default_factory=dict)
    rows: list[int] | None = None  # 0-based positions; None when the defect is column-wide

    def to_json(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "source_column": self.column.name if self.column else None,
            "target_columns": self.target_columns,
            "affected_rows": self.affected_rows,
            "rows": None if self.rows is None else row_numbers(self.rows),
            "details": self.details,
        }


@dataclass
class Work:
    """Mutable state of one table while corruptions are applied."""

    schema: dict[str, Any]
    rng: random.Random
    fake: Faker
    columns: list[Column]
    defects: list[Defect] = field(default_factory=list)
    missing_targets: list[str] = field(default_factory=list)
    unmapped: list[Column] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.columns[0].values)

    @property
    def key(self) -> str:
        return key_column(self.schema)

    def spec(self, target: str) -> dict[str, Any]:
        return next(c for c in self.schema["columns"] if c["name"] == target)

    def candidates(self, predicate: Callable[[dict[str, Any], Column], bool]) -> list[Column]:
        """Unclaimed single-target columns whose target spec satisfies ``predicate``."""
        return [
            col
            for col in self.columns
            if not col.claimed and len(col.targets) == 1 and predicate(self.spec(col.target), col)
        ]

    def pick(self, corruption: str, columns: list[Column]) -> Column:
        if not columns:
            raise InfeasibleCorruptionError(
                f"{corruption}: no eligible column left in {self.schema['table']}"
            )
        col = self.rng.choice(columns)
        col.claimed = True
        return col

    def sample_rows(self, col: Column, low: float, high: float) -> list[int]:
        """Pick a random fraction in [low, high] of the column's non-null rows."""
        filled = [i for i, v in enumerate(col.values) if v != ""]
        k = max(1, round(len(filled) * self.rng.uniform(low, high)))
        return sorted(self.rng.sample(filled, min(k, len(filled))))


@dataclass
class CorruptedTable:
    header: list[str]
    rows: list[list[str]]
    answer_key: dict[str, Any]


# --- Corruptions -------------------------------------------------------------------------


def _duplicates(w: Work) -> None:
    """Repeat ~3% of rows at random positions; about half differ by casing or whitespace."""
    n = w.n_rows
    n_dupes = max(1, round(n * 0.03))
    sources = sorted(w.rng.sample(range(n), min(n_dupes, n)))
    order = list(range(n))
    for src in sources:
        order.insert(w.rng.randrange(len(order) + 1), src)
    is_dupe = [False] * len(order)
    seen: set[int] = set()
    for pos, src in enumerate(order):
        # The first occurrence keeps clean values; later ones are the duplicates.
        is_dupe[pos] = src in seen
        seen.add(src)
    for col in w.columns:
        col.values = [col.values[i] for i in order]

    # Free-text columns (no allowed values) are the only ones perturbed, so the value-level
    # corruptions that run later never see a perturbed cell in a column they count.
    text_cols = [
        col
        for col in w.columns
        if w.spec(col.target)["type"] == "varchar" and "allowed_values" not in w.spec(col.target)
    ]
    perturbed = 0
    dupe_positions = [p for p, dupe in enumerate(is_dupe) if dupe]
    for pos in dupe_positions:
        if w.rng.random() >= 0.5:
            continue
        col = w.rng.choice(text_cols)
        value = col.values[pos]
        options = [
            v for v in (value.upper(), value.lower(), f" {value}", f"{value}  ") if v != value
        ]
        if value and options:
            col.values[pos] = w.rng.choice(options)
            perturbed += 1
    first_position = {}
    for pos, src in enumerate(order):
        first_position.setdefault(src, pos)
    key_col = next(col for col in w.columns if col.target == w.key)
    w.defects.append(
        Defect(
            DefectType.DUPLICATE_ROWS,
            key_col,
            [w.key],
            len(dupe_positions),
            {
                "exact": len(dupe_positions) - perturbed,
                "with_casing_or_whitespace": perturbed,
                "duplicate_of": [
                    [pos + 1, first_position[order[pos]] + 1] for pos in dupe_positions
                ],
            },
            rows=dupe_positions,
        )
    )


def _null_variants(w: Work) -> None:
    """Render empty cells of a nullable column as a mix of null tokens."""
    col = w.pick(
        "null_variants",
        w.candidates(lambda spec, c: not spec["required"] and "" in c.values),
    )
    tokens: Counter[str] = Counter()
    rows = []
    for i, value in enumerate(col.values):
        if value == "":
            token = w.rng.choice(NULL_TOKENS)
            col.values[i] = token
            tokens[token] += 1
            if token:
                rows.append(i)
    w.defects.append(
        Defect(
            DefectType.NULL_VARIANTS,
            col,
            [col.target],
            len(rows),
            {"tokens": dict(sorted(tokens.items()))},
            rows=rows,
        )
    )


def _noisy_number(w: Work, value: str, kind: str) -> str:
    if kind == "integer":
        n = int(value)
        options = [f"{n}.0", f"{n:03d}", f" {n} ", f"{n} units"]
    else:
        d = Decimal(value)
        options = [f"${d:,.2f}", f"{d:,.2f}", f"USD {d:.2f}", f"{d.normalize():f}", f" {d} "]
    return w.rng.choice([o for o in options if o != value])


def _type_noise(w: Work) -> None:
    """Format numbers and booleans inconsistently; a few cells become unparseable junk."""
    col = w.pick(
        "type_noise",
        w.candidates(lambda spec, _: spec["type"] in ("integer", "boolean", "decimal(10,2)")),
    )
    kind = w.spec(col.target)["type"]
    rows = w.sample_rows(col, 0.1, 0.4)
    junk = []
    for i in rows:
        value = col.values[i]
        if w.rng.random() < 0.02:
            col.values[i] = w.rng.choice(JUNK_TOKENS)
            junk.append(i)
        elif kind == "boolean":
            col.values[i] = w.rng.choice(TRUE_VARIANTS if value == "true" else FALSE_VARIANTS)
        else:
            col.values[i] = _noisy_number(w, value, kind)
    w.defects.append(
        Defect(
            DefectType.TYPE_NOISE,
            col,
            [col.target],
            len(rows),
            {"unparseable_rows": row_numbers(junk)},
            rows=rows,
        )
    )


def _value_drift(w: Work) -> None:
    """Replace categorical values with case variants and synonyms (Pro, PRO, professional)."""
    col = w.pick("value_drift", w.candidates(lambda spec, _: "allowed_values" in spec))
    rows = w.sample_rows(col, 0.1, 0.4)
    variants: dict[str, str] = {}
    for i in rows:
        value = col.values[i]
        options = (value.title(), value.upper(), f" {value} ", *SYNONYMS.get(value, ()))
        variant = w.rng.choice([o for o in options if o != value])
        col.values[i] = variant
        variants[variant] = value
    w.defects.append(
        Defect(
            DefectType.VALUE_DRIFT,
            col,
            [col.target],
            len(rows),
            {"variants": dict(sorted(variants.items()))},
            rows=rows,
        )
    )


def _mixed_date_formats(w: Work) -> None:
    """Rewrite some ISO dates as MM/DD/YYYY and possibly one more unambiguous format."""
    col = w.pick(
        "mixed_date_formats",
        w.candidates(
            lambda spec, c: spec["type"] == "date" and c.values.count("") < len(c.values) / 2
        ),
    )
    formats = ["MM/DD/YYYY"]
    if w.rng.random() < 0.5:
        formats.append(w.rng.choice(SECONDARY_DATE_FORMATS))
    rows = w.sample_rows(col, 0.1, 0.4)
    for i in rows:
        col.values[i] = DATE_FORMATS[w.rng.choice(formats)](date.fromisoformat(col.values[i]))
    counts = Counter(_date_format_of(v) for v in col.values if v != "")
    w.defects.append(
        Defect(
            DefectType.MIXED_DATE_FORMATS,
            col,
            [col.target],
            len(rows),
            {"formats": dict(sorted(counts.items()))},
            rows=rows,
        )
    )


def _date_format_of(value: str) -> str:
    patterns = {
        "YYYY-MM-DD": r"\d{4}-\d{2}-\d{2}",
        "MM/DD/YYYY": r"\d{2}/\d{2}/\d{4}",
        "DD Mon YYYY": r"\d{2} [A-Z][a-z]{2} \d{4}",
        "YYYYMMDD": r"\d{8}",
        "Month DD, YYYY": r"[A-Z][a-z]+ \d{2}, \d{4}",
    }
    return next(name for name, pattern in patterns.items() if re.fullmatch(pattern, value))


def _merge_columns(w: Work) -> None:
    """Join a plausible pair of columns into one with a separator, e.g. plan|status."""
    free = {col.target: col for col in w.candidates(lambda spec, _: True)}
    pairs = [pair for pair in MERGE_PAIRS[w.schema["table"]] if all(t in free for t in pair)]
    if not pairs:
        raise InfeasibleCorruptionError("merge_columns: no mergeable pair of free columns")
    first, second = (free[t] for t in w.rng.choice(pairs))
    separator = next(
        (
            sep
            for sep in w.rng.sample(["|", ";", " / "], 3)
            if not any(sep in v for v in first.values + second.values)
        ),
        None,
    )
    if separator is None:
        raise InfeasibleCorruptionError("merge_columns: every separator occurs in the values")
    merged = Column(
        name=f"{first.target}_{second.target}",
        values=[f"{a}{separator}{b}" for a, b in zip(first.values, second.values, strict=True)],
        targets=[first.target, second.target],
        separator=separator,
        claimed=True,
    )
    w.columns[w.columns.index(first)] = merged
    w.columns.remove(second)
    w.defects.append(
        Defect(
            DefectType.MERGED_COLUMN,
            merged,
            list(merged.targets),
            w.n_rows,
            {"separator": separator},
        )
    )


def _drop_required(w: Work) -> None:
    """Remove a required, non-key column from the file."""
    col = w.pick(
        "drop_required",
        w.candidates(lambda spec, _: spec["required"] and not spec.get("unique")),
    )
    w.columns.remove(col)
    w.missing_targets.append(col.target)
    w.defects.append(Defect(DefectType.MISSING_REQUIRED_COLUMN, None, [col.target], w.n_rows))


def _extra_value(w: Work, name: str, row: int) -> str:
    rng, fake = w.rng, w.fake
    match name:
        case "legacy_notes":
            return fake.sentence(nb_words=6) if rng.random() < 0.3 else ""
        case "import_batch":
            return f"B{rng.randint(1, 40):03d}"
        case "sales_rep":
            return fake.name()
        case "row_checksum":
            return hashlib.md5(f"{w.schema['table']}:{row}".encode()).hexdigest()[:12]
        case "region_code":
            return rng.choice(["NA", "EMEA", "APAC", "LATAM"])
        case "crm_lead_score":
            return str(rng.randint(0, 100))
        case "last_modified_by":
            return fake.user_name()
    raise ValueError(f"unknown extra column {name}")


def _extra_columns(w: Work) -> None:
    """Add one to three columns that have no target."""
    for name in w.rng.sample(EXTRA_COLUMNS, w.rng.randint(1, 3)):
        col = Column(name, [_extra_value(w, name, r) for r in range(w.n_rows)], [], claimed=True)
        w.columns.insert(w.rng.randrange(len(w.columns) + 1), col)
        w.unmapped.append(col)


def _cryptic(target: str, spec: dict[str, Any]) -> str:
    parts = [ABBREVIATIONS.get(word, word[:4]) for word in target.split("_")]
    name = "_".join(p for p in parts if p)
    if "allowed_values" in spec:
        name += "_cd"
    elif spec["type"] == "boolean":
        name += "_flg"
    return name


def _cryptic_names(w: Work) -> None:
    """Rename one to three mapped columns to legacy codes (plan -> pln_cd) or opaque fld_NN."""
    mapped = [col for col in w.columns if col.targets]
    for col in w.rng.sample(mapped, min(len(mapped), w.rng.randint(1, 3))):
        if w.rng.random() < 0.25:
            col.name = f"fld_{w.columns.index(col) + 1:02d}"
        else:
            col.name = "_".join(_cryptic(t, w.spec(t)) for t in col.targets)
        col.cryptic = True


def _style(words: str, style: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+|[#$?]", words)
    match style:
        case "upper_snake":
            return "_".join(t.upper() for t in tokens if t.isalnum())
        case "camel":
            alnum = [t for t in tokens if t.isalnum()]
            return alnum[0].lower() + "".join(t.capitalize() for t in alnum[1:])
    return words


def _rename_columns(w: Work) -> None:
    """Give every mapped column that is not cryptic a human-style name (Sign Up Dt)."""
    for col in w.columns:
        if not col.targets or col.cryptic:
            continue
        label = " / ".join(w.rng.choice(RENAMES[t]) for t in col.targets)
        col.name = _style(label, w.rng.choice(["as_is", "as_is", "upper_snake", "camel"]))


CORRUPTION_FUNCS: dict[str, Callable[[Work], None]] = {
    "duplicates": _duplicates,
    "null_variants": _null_variants,
    "type_noise": _type_noise,
    "value_drift": _value_drift,
    "mixed_date_formats": _mixed_date_formats,
    "merge_columns": _merge_columns,
    "drop_required": _drop_required,
    "extra_columns": _extra_columns,
    "cryptic_names": _cryptic_names,
    "rename_columns": _rename_columns,
}


def _dedupe_names(columns: list[Column]) -> None:
    seen: Counter[str] = Counter()
    for col in columns:
        seen[col.name] += 1
        if seen[col.name] > 1:
            col.name = f"{col.name}_{seen[col.name]}"


# --- Public API --------------------------------------------------------------------------


def corrupt_table(
    clean_rows: list[dict[str, str]],
    schema: dict[str, Any],
    seed: int,
    corruptions: Sequence[str],
) -> CorruptedTable:
    """Apply ``corruptions`` to clean rows (all values as CSV strings, nulls as "")."""
    unknown = set(corruptions) - set(CORRUPTIONS)
    if unknown:
        raise ValueError(f"Unknown corruptions: {sorted(unknown)}")
    if len(set(corruptions)) != len(corruptions):
        raise ValueError(f"Duplicate corruptions in {list(corruptions)}")
    if not clean_rows:
        raise ValueError("No clean rows to corrupt")

    fake = Faker("en_US")
    fake.seed_instance(seed)
    columns = [
        Column(name, [row[name] for row in clean_rows], [name]) for name in column_names(schema)
    ]
    work = Work(schema=schema, rng=random.Random(seed), fake=fake, columns=columns)
    for name in APPLY_ORDER:
        if name in corruptions:
            CORRUPTION_FUNCS[name](work)
    _dedupe_names(work.columns)

    mappings = []
    for col in work.columns:
        for part, target in enumerate(col.targets, start=1):
            entry: dict[str, Any] = {"source_column": col.name, "target_column": target}
            if col.separator is not None:
                entry["merged"] = {"separator": col.separator, "part": part}
            mappings.append(entry)
    answer_key = {
        "table": schema["table"],
        "seed": seed,
        "corruptions": list(corruptions),
        "names_changed": bool(set(corruptions) & set(NAMING_CORRUPTIONS)),
        "key_column": key_column(schema),
        "clean_rows": len(clean_rows),
        "messy_rows": work.n_rows,
        "mappings": mappings,
        "unmapped_source_columns": [col.name for col in work.unmapped],
        "missing_target_columns": work.missing_targets,
        "defects": [d.to_json() for d in work.defects],
    }
    header = [col.name for col in work.columns]
    rows = [list(values) for values in zip(*(col.values for col in work.columns), strict=True)]
    return CorruptedTable(header, rows, answer_key)


def read_clean_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def sample_clean(rows: list[dict[str, str]], n: int, seed: int) -> list[dict[str, str]]:
    """Deterministic sample of ``n`` rows, kept in source order."""
    indices = sorted(random.Random(seed).sample(range(len(rows)), min(n, len(rows))))
    return [rows[i] for i in indices]


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_case(
    case_dir: Path,
    case_id: str,
    clean_rows: list[dict[str, str]],
    schema: dict[str, Any],
    seed: int,
    corruptions: Sequence[str],
) -> dict[str, Any]:
    """Corrupt ``clean_rows`` and write messy.csv, clean.csv and answer_key.json."""
    result = corrupt_table(clean_rows, schema, seed, corruptions)
    case_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(case_dir / "messy.csv", result.header, result.rows)
    header = column_names(schema)
    _write_csv(case_dir / "clean.csv", header, [[row[c] for c in header] for row in clean_rows])
    answer_key = {
        "case_id": case_id,
        "messy_file": "messy.csv",
        "clean_file": "clean.csv",
        **result.answer_key,
    }
    (case_dir / "answer_key.json").write_text(
        json.dumps(answer_key, indent=2) + "\n", encoding="utf-8"
    )
    return answer_key


def plan_cases(seed: int, per_table: int = 10) -> list[tuple[str, str, int, list[str]]]:
    """Return (case_id, table, case_seed, corruptions) for every case.

    Each table's cases cycle through every corruption type as the primary corruption, so with
    ``per_table >= len(CORRUPTIONS)`` each type appears at least once per table. Up to three
    extra corruptions are added at random, for one to four per case. At least two thirds of
    each table's cases change column names, the hard part of mapping.
    """
    rng = random.Random(seed)
    plan = []
    for table in TABLES:
        primaries = list(CORRUPTIONS)
        rng.shuffle(primaries)
        must_rename = set(rng.sample(range(per_table), math.ceil(per_table * 2 / 3)))
        for i in range(per_table):
            primary = primaries[i % len(primaries)]
            others = [c for c in CORRUPTIONS if c != primary]
            extras = rng.sample(others, rng.randint(0, 3))
            if i in must_rename and not {primary, *extras} & set(NAMING_CORRUPTIONS):
                extras = [rng.choice(NAMING_CORRUPTIONS), *extras][:3]
            # Renames never fail, so keep them ahead of extras that build_cases may drop.
            extras.sort(key=lambda c: c not in NAMING_CORRUPTIONS)
            plan.append((f"{table}_{i + 1:02d}", table, rng.randrange(2**31), [primary, *extras]))
    return plan


def build_cases(
    clean_dir: Path, out_dir: Path, seed: int, rows: int = 2000, per_table: int = 10
) -> list[dict[str, Any]]:
    """Write every planned case. Infeasible extra corruptions are dropped from the end."""
    clean = {table: read_clean_csv(clean_dir / f"{table}.csv") for table in TABLES}
    keys = []
    for case_id, table, case_seed, corruptions in plan_cases(seed, per_table):
        sample = sample_clean(clean[table], rows, case_seed)
        schema = load_schema(table)
        while True:
            try:
                keys.append(
                    write_case(out_dir / case_id, case_id, sample, schema, case_seed, corruptions)
                )
                break
            except InfeasibleCorruptionError:
                if len(corruptions) == 1:
                    raise
                corruptions = corruptions[:-1]
    return keys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create messy eval cases with answer keys.")
    sub = parser.add_subparsers(dest="command", required=True)

    all_cmd = sub.add_parser("all", help="build the full case set from the clean tables")
    all_cmd.add_argument("--per-table", type=int, default=10)

    case_cmd = sub.add_parser("case", help="build one case")
    case_cmd.add_argument("--table", required=True, choices=TABLES)
    case_cmd.add_argument("--corruptions", required=True, help="comma-separated list")
    case_cmd.add_argument("--case-id", required=True)

    for cmd in (all_cmd, case_cmd):
        cmd.add_argument("--seed", type=int, default=42)
        cmd.add_argument("--rows", type=int, default=2000)
        cmd.add_argument("--clean-dir", type=Path, default=Path("data/clean"))
        cmd.add_argument("--out", type=Path, default=Path("evals/cases"))
    args = parser.parse_args(argv)

    if args.command == "all":
        keys = build_cases(args.clean_dir, args.out, args.seed, args.rows, args.per_table)
        counts = Counter(c for key in keys for c in key["corruptions"])
        print(f"Wrote {len(keys)} cases to {args.out}/")
        for name in CORRUPTIONS:
            print(f"  {name:<20} {counts[name]:>3} cases")
    else:
        corruptions = [c.strip() for c in args.corruptions.split(",") if c.strip()]
        rows = read_clean_csv(args.clean_dir / f"{args.table}.csv")
        sample = sample_clean(rows, args.rows, args.seed)
        schema = load_schema(args.table)
        write_case(args.out / args.case_id, args.case_id, sample, schema, args.seed, corruptions)
        print(f"Wrote {args.out / args.case_id}/")


if __name__ == "__main__":
    main()
