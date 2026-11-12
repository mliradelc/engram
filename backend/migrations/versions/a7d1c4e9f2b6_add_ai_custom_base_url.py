"""add app_config.ai_custom_base_url

The base URL for the user-supplied custom AI provider (any OpenAI-compatible
endpoint). Kept separate from ai_local_base_url so switching between a local
server and a custom gateway does not carry one address over to the other.

Mirrors the database.py reconciler path used by frozen builds (which skip
Alembic); the two must stay in agreement.

Revision ID: a7d1c4e9f2b6
Revises: c6e3a8d2f5b1
Create Date: 2026-11-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migration_guards import add_column_if_missing

# revision identifiers, used by Alembic.
revision: str = "a7d1c4e9f2b6"
down_revision: str | Sequence[str] | None = "c6e3a8d2f5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    add_column_if_missing(
        "app_config",
        sa.Column("ai_custom_base_url", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    with op.batch_alter_table("app_config", schema=None) as batch_op:
        batch_op.drop_column("ai_custom_base_url")
