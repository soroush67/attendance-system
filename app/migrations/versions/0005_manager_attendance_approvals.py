"""Add isolated, auditable two-person approval workflow for manager attendance.

Revision ID: 0005_manager_approvals
Revises: 0004_management_manual

ADDITIVE ONLY: no employee, punch, import, correction or report data changed.
"""

from alembic import op
import sqlalchemy as sa


revision = '0005_manager_approvals'
down_revision = '0004_management_manual'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'manager_attendance_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.Column('proposed_first', sa.DateTime(timezone=True), nullable=False),
        sa.Column('proposed_last', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('requested_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expected_revision_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(16), server_default='pending', nullable=False),
        sa.Column('resolved_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_reason', sa.String(500), server_default='', nullable=False),
        sa.Column('correction_id', sa.Integer(), sa.ForeignKey('attendance_corrections.id'), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'cancelled')",
                           name='ck_manager_requests_status'),
    )
    op.create_index('ix_manager_requests_status_created', 'manager_attendance_requests',
                    ['status', 'requested_at'])
    op.create_index('ix_manager_requests_employee_day', 'manager_attendance_requests',
                    ['employee_id', 'local_day'])
    op.create_index('uq_manager_requests_one_pending', 'manager_attendance_requests',
                    ['employee_id', 'local_day'], unique=True,
                    postgresql_where=sa.text("status = 'pending'"))


def downgrade():
    # Never erase pending approvals and immutable review history automatically.
    raise RuntimeError('Manager approval history must not be destroyed during rollback')
