"""Work-hours feature: work_settings, leave_days, attendance_corrections.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'work_settings',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('standard_hours', sa.Integer, nullable=False, server_default='8'),
        sa.Column('work_days', sa.String(60), nullable=False, server_default='sat,sun,mon,tue,wed,thu'),
        sa.Column('overtime_days', sa.String(60), nullable=False, server_default='fri'),
    )
    # seed the single settings row
    op.execute("INSERT INTO work_settings (id, standard_hours, work_days, overtime_days) "
               "VALUES (1, 8, 'sat,sun,mon,tue,wed,thu', 'fri')")

    op.create_table(
        'leave_days',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('employee_id', sa.Integer, sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.Column('leave_type', sa.String(30), nullable=False, server_default='مرخصی'),
        sa.Column('actor_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.UniqueConstraint('employee_id', 'local_day', name='uq_leave_employee_day'),
    )
    op.create_index('ix_leave_days_local_day', 'leave_days', ['local_day'])

    op.create_table(
        'attendance_corrections',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('employee_id', sa.Integer, sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('actor_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.Column('punch_time', sa.String(8), nullable=False),
        sa.Column('punch_type', sa.String(10), nullable=False),
        sa.Column('reason', sa.String(300), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    op.create_index('ix_corrections_employee_day', 'attendance_corrections',
                    ['employee_id', 'local_day'])


def downgrade() -> None:
    op.drop_index('ix_corrections_employee_day', 'attendance_corrections')
    op.drop_table('attendance_corrections')
    op.drop_index('ix_leave_days_local_day', 'leave_days')
    op.drop_table('leave_days')
    op.drop_table('work_settings')
