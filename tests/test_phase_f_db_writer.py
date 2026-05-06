import json
from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ToolManifest
from agentend.db.session import session_scope


def test_db_writer_executes_queries_and_writes_rows(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    database = "data/user.sqlite"

    created = runner.invoke(
        app,
        [
            "tools",
            "test",
            "db.execute",
            "--home",
            str(home),
            "--input",
            json.dumps({"database": database, "sql": "create table items (id integer, name text)"}),
        ],
    )
    inserted = runner.invoke(
        app,
        [
            "tools",
            "test",
            "db.write_rows",
            "--home",
            str(home),
            "--input",
            json.dumps({"database": database, "table": "items", "rows": [{"id": 1, "name": "alpha"}]}),
        ],
    )
    queried = runner.invoke(
        app,
        ["tools", "test", "db.query", "--home", str(home), "--input", json.dumps({"database": database, "sql": "select * from items"})],
    )

    assert created.exit_code == 0
    assert inserted.exit_code == 0
    assert queried.exit_code == 0
    assert "alpha" in queried.output
    with session_scope(home) as session:
        query_manifest = session.get(ToolManifest, "db.query")
        write_manifest = session.get(ToolManifest, "db.write_rows")
        assert query_manifest is not None
        assert query_manifest.side_effect == "local_read"
        assert write_manifest is not None
        assert write_manifest.side_effect == "local_write"
