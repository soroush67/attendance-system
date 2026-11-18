"""Add manager-only manual attendance classification and provenance.

Revision ID: 0004_management_manual
Revises: 0003_reconciliation

Existing employees remain ordinary staff. Existing corrections remain labeled
admin_correction. No punch, import, correction or report row is modified.
"""

from alembic import op
import sqlalchemy as sa


revision = '0004_management_manual'
down_revision = '0003_reconciliation'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('employees', sa.Column('attendance_group', sa.String(20),
                                         nullable=False, server_default='staff'))
    op.create_check_constraint('ck_employees_attendance_group', 'employees',
                               "attendance_group IN ('staff', 'management')")
    op.create_index('ix_employees_attendance_group_status', 'employees',
                    ['attendance_group', 'status'])
    op.add_column('attendance_corrections', sa.Column('origin', sa.String(24),
                                                      nullable=False, server_default='admin_correction'))
    op.create_check_constraint('ck_attendance_corrections_origin', 'attendance_corrections',
                               "origin IN ('admin_correction', 'manager_manual')")


def downgrade():
    # A downgrade would erase manager classification and manual-entry
    # provenance while keeping the associated human decisions. Fail closed.
    raise RuntimeError('Management manual-attendance history must not be discarded during rollback')
