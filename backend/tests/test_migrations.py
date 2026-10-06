import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect


def test_migrations_upgrade_downgrade_and_upgrade(tmp_path):
    path = tmp_path / "migration.db"
    env = {**os.environ, "ENVIRONMENT": "testing", "DATABASE_URL": f"sqlite+aiosqlite:///{path}"}
    for target in ("head", "base", "head"):
        command = "downgrade" if target == "base" else "upgrade"
        subprocess.run([sys.executable, "-m", "alembic", command, target], env=env, check=True, capture_output=True)
        engine = create_engine(f"sqlite:///{path}")
        assert ("research_jobs" in inspect(engine).get_table_names()) == (target == "head")
        engine.dispose()
