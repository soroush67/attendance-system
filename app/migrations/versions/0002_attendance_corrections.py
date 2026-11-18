"""Append-only, auditable manual attendance corrections.

Revision ID: 0002_corrections
Revises: 0001_initial

ADDITIVE ONLY: no device logs, reports, employees or uploads are modified.
"""

from alembic import op
import sqlalchemy as sa


revision = '0002_corrections'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'attendance_corrections',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('override_first', sa.DateTime(timezone=True), nullable=True),
        sa.Column('override_last', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('set', 'revoke')", name='ck_attendance_corrections_action'),
        sa.CheckConstraint(
            "(action = 'set' AND (override_first IS NOT NULL OR override_last IS NOT NULL)) "
            "OR (action = 'revoke' AND override_first IS NULL AND override_last IS NULL)",
            name='ck_attendance_corrections_fields',
        ),
    )
    op.create_index(
        'ix_attendance_corrections_employee_day_revision',
        'attendance_corrections', ['employee_id', 'local_day', 'id'],
    )
    op.create_index('ix_attendance_corrections_local_day', 'attendance_corrections', ['local_day'])


def downgrade():
    # Preserving an immutable correction audit trail takes priority over a
    # destructive downgrade. Roll back the web image; keep this additive table.
    raise RuntimeError('Corrections have an append-only audit trail; schema downgrade is intentionally disabled.')
