# Scorecard: anthropic/claude-haiku-5.5 (raha)

20261127T181747Z · 3 cases · review threshold 0.8 · 0 failed runs · 0 repairs · 14 model calls · 220619 tokens · 170.5s

| Metric | Value |
| --- | --- |
| Mapping accuracy | 100.0% |
| Mapping accuracy, names changed | 100.0% |
| Mapping accuracy, names unchanged | 100.0% |
| Defect recall | n/a |
| Defect precision | 0.0% |
| Cell accuracy | 88.3% |
| Cell accuracy of the dirty file as is | 84.5% |
| Escalation rate | 0.0% |
| Bad auto-approvals | 0 |
| Build success | 100.0% |
| Consistency | n/a |

## Runs

| Case | Run | Corruptions | Status | Mapping | Defects found | Flagged correct | Escalated | Bad auto | Loaded / rejected | Cells | Repairs | Calls | Seconds |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| raha_beers | 1 | raha | done | 11/11 | 0/0 | 0/3 | 0/11 | 0 | 2410 / 0 | 96.4% | 0 | 5 | 28.29 |
| raha_flights | 1 | raha | done | 7/7 | 0/0 | 0/6 | 0/7 | 0 | 2325 / 51 | 70.9% | 0 | 5 | 48.63 |
| raha_hospital | 1 | raha | done | 20/20 | 0/0 | 0/9 | 0/20 | 0 | 936 / 64 | 92.2% | 0 | 4 | 93.57 |

## Errors

- **raha_beers** spurious defects: null_variants@ibu, type_noise@abv, type_noise@ounces
- **raha_flights** spurious defects: null_variants@act_dep_time, null_variants@act_arr_time, null_variants@sched_dep_time, mixed_date_formats@sched_arr_time, type_noise@act_dep_time, type_noise@act_arr_time
- **raha_hospital** spurious defects: type_noise@zip, type_noise@phone, type_noise@emergency_service, type_noise@provider_number, type_noise@owner, type_noise@condition, type_noise@type, type_noise@state, type_noise@measure_name
