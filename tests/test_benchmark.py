from __future__ import annotations

import csv
import json

from evals.benchmark import build_case


def test_a_raha_pair_becomes_an_eval_case_with_a_positional_mapping(tmp_path):
    header = "tuple_id,src,flight,sched_dep_time,act_dep_time,sched_arr_time,act_arr_time\n"
    dirty = header + "1,aa,AA-1-IAH-ORD,7:10 a.m.,7:16 a.m.,9:40 a.m.,\n2,ua,UA-2,1:00 p.m.,,,\n"
    clean = (
        header + "1,aa,AA-1-IAH-ORD,7:10 a.m.,7:16 a.m.,9:40 a.m.,9:32 a.m.\n"
        "2,ua,UA-2,1:00 p.m.,1:05 p.m.,,\n"
    )
    key = build_case("flights", dirty, clean, tmp_path)

    case = tmp_path / "raha_flights"
    assert json.loads((case / "answer_key.json").read_text()) == key
    assert (case / "messy.csv").read_text() == dirty
    assert key["mappings"][2] == {"source_column": "flight", "target_column": "flight"}
    assert (key["key_column"], key["messy_rows"], key["defects"]) == ("tuple_id", 2, [])
    assert key["baseline_cells"] == [12, 14]  # two cells were cleaned
    with (case / "clean.csv").open() as fh:
        assert next(csv.reader(fh))[0] == "tuple_id"
