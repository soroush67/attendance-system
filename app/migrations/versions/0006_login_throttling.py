"""Add PostgreSQL-backed login rate limits without modifying business data.

Revision ID: 0006_login_throttling
Revises: 0005_manager_approvals

ADDITIVE ONLY: no changes to existing users, sessions, employee data or reports.
"""

from alembic import op
import sqlalchemy as sa


revision = '0006_login_throttling'
down_revision = '0005_manager_approvals'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'login_rate_limits',
        sa.Column('key_digest', sa.String(64), primary_key=True),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('window_started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('blocked_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('failures >= 0', name='ck_login_rate_limits_failures'),
    )
    op.create_index('ix_login_rate_limits_last_seen', 'login_rate_limits', ['last_seen_at'])


def downgrade():
    # Retain security and forensic history. A web-only rollback can run with
    # this harmless extra table; destroying persisted counters is forbidden.
    raise RuntimeError('Login rate-limit history must not be removed automatically')
