"""The fixed defect vocabulary shared by the agent, the app database, and eval answer keys."""

from __future__ import annotations

from enum import StrEnum


class DefectType(StrEnum):
    DUPLICATE_ROWS = "duplicate_rows"
    NULL_VARIANTS = "null_variants"
    TYPE_NOISE = "type_noise"
    VALUE_DRIFT = "value_drift"
    MIXED_DATE_FORMATS = "mixed_date_formats"
    MERGED_COLUMN = "merged_column"
    MISSING_REQUIRED_COLUMN = "missing_required_column"


DEFECT_DESCRIPTIONS = {
    DefectType.DUPLICATE_ROWS: "rows repeated on the key, maybe differing in casing or whitespace",
    DefectType.NULL_VARIANTS: "missing values written as text such as NULL, n/a or -",
    DefectType.TYPE_NOISE: "numbers or booleans formatted inconsistently ($1,200.00, 1200, Y)",
    DefectType.VALUE_DRIFT: "categorical values outside the allowed set (Pro, PRO, professional)",
    DefectType.MIXED_DATE_FORMATS: "one date column using several formats",
    DefectType.MERGED_COLUMN: "one source column holding two target columns and a separator",
    DefectType.MISSING_REQUIRED_COLUMN: "a required target column with no source column",
}
