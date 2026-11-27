# Scorecard: anthropic/claude-haiku-5.5

20261127T180503Z · 30 cases · review threshold 0.8 · 0 failed runs · 0 repairs · 76 model calls · 674715 tokens · 466.6s

| Metric | Value |
| --- | --- |
| Mapping accuracy | 100.0% |
| Mapping accuracy, names changed | 100.0% |
| Mapping accuracy, names unchanged | 100.0% |
| Defect recall | 100.0% |
| Defect precision | 82.9% |
| Cell accuracy | 86.3% |
| Escalation rate | 2.2% |
| Bad auto-approvals | 0 |
| Build success | 100.0% |
| Consistency | n/a |

## Runs

| Case | Run | Corruptions | Status | Mapping | Defects found | Flagged correct | Escalated | Bad auto | Loaded / rejected | Cells | Repairs | Calls | Seconds |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| customers_01 | 1 | merge_columns, rename_columns | done | 6/6 | 1/1 | 1/2 | 0/7 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 12.67 |
| invoices_01 | 1 | cryptic_names, rename_columns, null_variants, extra_columns | done | 10/10 | 1/1 | 1/1 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 15.07 |
| subscriptions_01 | 1 | null_variants, rename_columns, drop_required | done | 7/7 | 2/2 | 2/2 | 1/8 | 0 | 0 / 2000 | 0.0% | 0 | 2 | 12.51 |
| customers_02 | 1 | type_noise, cryptic_names, extra_columns | done | 10/10 | 1/1 | 1/2 | 0/7 | 0 | 1992 / 8 | 99.6% | 0 | 2 | 11.09 |
| invoices_02 | 1 | type_noise, cryptic_names | done | 8/8 | 1/1 | 1/2 | 0/8 | 0 | 1985 / 15 | 99.2% | 0 | 3 | 16.3 |
| subscriptions_02 | 1 | drop_required, rename_columns, value_drift, duplicates | done | 7/7 | 3/3 | 3/4 | 1/8 | 0 | 0 / 2060 | 0.0% | 0 | 4 | 20.62 |
| customers_03 | 1 | mixed_date_formats, merge_columns | done | 6/6 | 2/2 | 2/2 | 0/7 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 11.08 |
| invoices_03 | 1 | null_variants, rename_columns, value_drift | done | 8/8 | 2/2 | 2/2 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 3 | 18.13 |
| subscriptions_03 | 1 | extra_columns, rename_columns | done | 9/9 | 0/0 | 0/1 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 12.3 |
| customers_04 | 1 | extra_columns, mixed_date_formats, type_noise | done | 10/10 | 2/2 | 2/3 | 0/7 | 0 | 1992 / 8 | 99.6% | 0 | 2 | 12.28 |
| invoices_04 | 1 | rename_columns, cryptic_names, extra_columns | done | 9/9 | 0/0 | 0/0 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 13.23 |
| subscriptions_04 | 1 | mixed_date_formats, merge_columns, null_variants | done | 7/7 | 3/3 | 4/4 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 14.0 |
| customers_05 | 1 | null_variants, rename_columns | done | 7/7 | 1/1 | 1/1 | 0/7 | 0 | 2000 / 0 | 100.0% | 0 | 3 | 16.91 |
| invoices_05 | 1 | extra_columns, rename_columns, duplicates, type_noise | done | 9/9 | 2/2 | 4/5 | 0/8 | 0 | 1991 / 69 | 99.6% | 0 | 2 | 17.86 |
| subscriptions_05 | 1 | merge_columns, null_variants | done | 7/7 | 2/2 | 2/3 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 12.81 |
| customers_06 | 1 | value_drift | done | 7/7 | 1/1 | 1/1 | 0/7 | 0 | 1934 / 66 | 96.7% | 0 | 2 | 14.55 |
| invoices_06 | 1 | mixed_date_formats, rename_columns | done | 8/8 | 1/1 | 1/1 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 11.52 |
| subscriptions_06 | 1 | rename_columns, merge_columns, extra_columns, mixed_date_formats | done | 10/10 | 2/2 | 3/3 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 13.47 |
| customers_07 | 1 | drop_required, rename_columns, duplicates | done | 6/6 | 2/2 | 3/3 | 1/7 | 0 | 0 / 2060 | 0.0% | 0 | 3 | 21.08 |
| invoices_07 | 1 | duplicates | done | 8/8 | 1/1 | 3/3 | 0/8 | 0 | 2000 / 60 | 100.0% | 0 | 4 | 21.04 |
| subscriptions_07 | 1 | value_drift, type_noise | done | 8/8 | 2/2 | 2/3 | 0/8 | 0 | 1987 / 13 | 99.4% | 0 | 2 | 15.95 |
| customers_08 | 1 | duplicates, cryptic_names | done | 7/7 | 1/1 | 1/1 | 0/7 | 0 | 2000 / 60 | 100.0% | 0 | 4 | 21.52 |
| invoices_08 | 1 | merge_columns, extra_columns | done | 10/10 | 1/1 | 1/1 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 3 | 15.28 |
| subscriptions_08 | 1 | type_noise, cryptic_names, merge_columns, value_drift | done | 7/7 | 3/3 | 4/6 | 0/8 | 0 | 1993 / 7 | 99.7% | 0 | 2 | 17.34 |
| customers_09 | 1 | rename_columns, value_drift, duplicates | done | 7/7 | 2/2 | 3/4 | 0/7 | 0 | 1960 / 100 | 98.0% | 0 | 3 | 19.9 |
| invoices_09 | 1 | drop_required, rename_columns, extra_columns, value_drift | done | 10/10 | 2/2 | 2/2 | 1/8 | 0 | 0 / 2000 | 0.0% | 0 | 3 | 17.95 |
| subscriptions_09 | 1 | cryptic_names, rename_columns, mixed_date_formats, merge_columns | done | 7/7 | 2/2 | 2/2 | 1/8 | 0 | 2000 / 0 | 100.0% | 0 | 3 | 17.91 |
| customers_10 | 1 | cryptic_names | done | 7/7 | 0/0 | 0/0 | 0/7 | 0 | 2000 / 0 | 100.0% | 0 | 2 | 10.49 |
| invoices_10 | 1 | value_drift, merge_columns, null_variants | done | 7/7 | 3/3 | 3/3 | 0/8 | 0 | 2000 / 0 | 100.0% | 0 | 3 | 16.94 |
| subscriptions_10 | 1 | duplicates, cryptic_names, mixed_date_formats | done | 8/8 | 2/2 | 3/3 | 0/8 | 0 | 2000 / 60 | 100.0% | 0 | 3 | 14.85 |

## Errors

- **customers_01** spurious defects: null_variants@Country Code
- **customers_02** spurious defects: null_variants@actv_flg
- **invoices_02** spurious defects: null_variants@amount_usd
- **subscriptions_02** spurious defects: value_drift@Tier
- **subscriptions_03** spurious defects: duplicate_rows@cust
- **customers_04** spurious defects: null_variants@is_active
- **invoices_05** spurious defects: null_variants@Total ($)
- **subscriptions_05** spurious defects: duplicate_rows@customer_id
- **subscriptions_07** spurious defects: null_variants@monthly_amount_usd
- **subscriptions_08** spurious defects: null_variants@monthly_amount_usd, duplicate_rows@customer_id
- **customers_09** spurious defects: null_variants@ctry
