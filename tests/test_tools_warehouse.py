from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from tools.profiling import list_files
from tools.warehouse import ToolError, Warehouse, load_file


def raw_tables(wh: Warehouse) -> list[str]:
    with wh.connect(read_only=True) as con:
        rows = con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'raw' ORDER BY 1"
        ).fetchall()
    return [r[0] for r in rows]


def test_loads_every_column_as_text_with_source_row_numbers(wh, load_csv):
    file = load_csv("id,amount,when\n3,1.50,2024-01-01\n1,,2024-02-01\n2,7,\n")
    assert file.row_count == 3
    with wh.connect(read_only=True) as con:
        types = con.execute(f"DESCRIBE raw.{file.table_name}").fetchall()
        rows = con.execute(f"SELECT * FROM raw.{file.table_name} ORDER BY _row").fetchall()
    assert [(t[0], t[1]) for t in types] == [
        ("_row", "BIGINT"),
        ("id", "VARCHAR"),
        ("amount", "VARCHAR"),
        ("when", "VARCHAR"),
    ]
    assert rows == [
        (1, "3", "1.50", "2024-01-01"),
        (2, "1", None, "2024-02-01"),
        (3, "2", "7", None),
    ]


def test_loading_the_same_content_twice_creates_nothing_new(wh, load_csv):
    first = load_csv("a,b\n1,2\n", name="one.csv")
    again = load_csv("a,b\n1,2\n", name="one.csv")
    renamed = load_csv("a,b\n1,2\n", name="other.csv")
    assert first == again == renamed
    assert raw_tables(wh) == ["one"]
    assert list_files(wh) == [first]


def test_same_stem_with_new_content_gets_a_new_table(wh, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "export.csv").write_text("x\n1\n")
    (tmp_path / "b" / "export.csv").write_text("x\n2\n")
    first = load_file(wh, tmp_path / "a" / "export.csv")
    second = load_file(wh, tmp_path / "b" / "export.csv")
    assert first.file_id != second.file_id
    assert (first.table_name, second.table_name) == ("export", "export_2")
    assert [f.file_id for f in list_files(wh)] == [first.file_id, second.file_id]


def test_table_names_are_sanitized(load_csv):
    assert load_csv("x\n1\n", name="2024 Export (final).csv").table_name == "f_2024_export_final"


def test_loads_parquet_as_text(wh, tmp_path):
    path = tmp_path / "typed.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT 42 AS n, DATE '2024-03-01' AS d, true AS b) TO '{path}' (FORMAT parquet)"
    )
    con.close()
    file = load_file(wh, path)
    with wh.connect(read_only=True) as ro:
        assert ro.execute(f"SELECT n, d, b FROM raw.{file.table_name}").fetchall() == [
            ("42", "2024-03-01", "true")
        ]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("data.xlsx", "x", "Unsupported file type"),
        ("empty.csv", "", "is empty"),
        ("header.csv", "a,b\n", "no data rows"),
        ("reserved.csv", "_row,a\n1,2\n", "reserved"),
    ],
)
def test_rejects_bad_files_with_a_readable_message(wh, tmp_path, name, content, message):
    path = tmp_path / name
    path.write_text(content)
    with pytest.raises(ToolError, match=message):
        load_file(wh, path)
    assert list_files(wh) == []


def test_rejects_missing_file(wh, tmp_path):
    with pytest.raises(ToolError, match="not found"):
        load_file(wh, tmp_path / "nope.csv")


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "", "x" * 65, "-leading"])
def test_rejects_unsafe_run_ids(tmp_path, run_id):
    with pytest.raises(ToolError, match="Invalid run_id"):
        Warehouse.for_run(run_id, tmp_path)


def test_unreadable_csv_is_a_tool_error_and_leaves_nothing_behind(wh, tmp_path):
    path = tmp_path / "latin1.csv"
    path.write_bytes("name\nJos\xe9\n".encode("latin-1"))
    with pytest.raises(ToolError, match=r"Could not read latin1\.csv"):
        load_file(wh, path)
    assert list_files(wh) == []


def test_warehouse_path_follows_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert Warehouse.for_run("r1").path == tmp_path / "runs" / "r1" / "warehouse.duckdb"


def test_empty_run_has_no_files_and_refuses_read_only_access(wh):
    assert list_files(wh) == []
    with pytest.raises(ToolError, match="no loaded files"), wh.connect(read_only=True):
        pass


def test_read_only_connection_blocks_writes_and_file_access(wh, load_csv, tmp_path: Path):
    """Defense in depth: even SQL that slipped past validation cannot change or leave the run."""
    file = load_csv("a\n1\n")
    outside = tmp_path / "outside.csv"
    outside.write_text("secret\n1\n")
    with wh.connect(read_only=True) as con:
        for statement in [
            f"DROP TABLE raw.{file.table_name}",
            f"DELETE FROM raw.{file.table_name}",
            "CREATE TABLE raw.x AS SELECT 1",
            f"SELECT * FROM read_csv('{outside}')",
            "SET enable_external_access = true",
        ]:
            with pytest.raises(duckdb.Error):
                con.execute(statement)
    assert list_files(wh) == [file]
