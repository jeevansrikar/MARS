"""Owner-filtered vector memory; no unbenchmarked approximate index.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "memory_entries", sa.Column("embedding", Vector(384).with_variant(sa.JSON(), "sqlite"), nullable=True)
    )
    op.add_column("memory_entries", sa.Column("embedding_model", sa.String(200), nullable=True))


def downgrade():
    op.drop_column("memory_entries", "embedding_model")
    op.drop_column("memory_entries", "embedding")
    # The shared vector extension may be used by other applications; retain it.
