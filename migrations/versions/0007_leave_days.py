"""Add leave_days table for admin-recorded employee leave.

Revision ID: 0007_leave_days
Revises: 0006_login_throttling

ADDITIVE ONLY: no existing tables or data are modified.
"""

from alembic import op
import sqlalchemy as sa


revision = '0007_leave_days'
down_revision = '0006_login_throttling'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'leave_days',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.Column('leave_type', sa.String(30), nullable=False, server_default='مرخصی'),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('employee_id', 'local_day', name='uq_leave_days_employee_day'),
    )
    op.create_index('ix_leave_days_local_day', 'leave_days', ['local_day'])


def downgrade():
    op.drop_index('ix_leave_days_local_day', table_name='leave_days')
    op.drop_table('leave_days')
