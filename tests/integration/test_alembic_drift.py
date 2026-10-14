"""Test verifying that Alembic migrations match SQLAlchemy models with zero drift."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from app.db.base import Base, engine


def test_alembic_upgrade_head_matches_models_zero_drift():
    """Verify that alembic upgrade head produces a schema matching SQLAlchemy Base.metadata with no drift."""
    root = Path(__file__).resolve().parent.parent.parent
    alembic_cfg = Config(str(root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(root / "migrations"))

    # Verify upgrade head runs cleanly
    command.upgrade(alembic_cfg, "head")

    # Compare migrated database schema against SQLAlchemy metadata
    with engine.connect() as conn:
        mc = MigrationContext.configure(conn)
        diff = compare_metadata(mc, Base.metadata)

    # Assert zero differences between migrated DB and declarative models
    assert diff == [], f"Detected schema drift between Alembic migrations and SQLAlchemy models: {diff}"
