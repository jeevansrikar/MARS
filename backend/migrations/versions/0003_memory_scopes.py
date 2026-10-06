"""Explicit session/user/organization memory visibility.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "organization_memberships",
        sa.Column(
            "organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_organization_memberships_user_id", "organization_memberships", ["user_id"])
    with op.batch_alter_table("research_jobs") as batch:
        batch.add_column(sa.Column("memory_scope", sa.String(16), nullable=False, server_default="user"))
        batch.add_column(sa.Column("organization_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_jobs_organization", "organizations", ["organization_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("memory_entries") as batch:
        batch.add_column(sa.Column("organization_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_memory_organization", "organizations", ["organization_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_memory_entries_organization_id", ["organization_id"])


def downgrade():
    with op.batch_alter_table("memory_entries") as batch:
        batch.drop_index("ix_memory_entries_organization_id")
        batch.drop_constraint("fk_memory_organization", type_="foreignkey")
        batch.drop_column("organization_id")
    with op.batch_alter_table("research_jobs") as batch:
        batch.drop_constraint("fk_jobs_organization", type_="foreignkey")
        batch.drop_column("organization_id")
        batch.drop_column("memory_scope")
    op.drop_index("ix_organization_memberships_user_id", table_name="organization_memberships")
    op.drop_table("organization_memberships")
    op.drop_table("organizations")
