"""0004 – hourly leave: add start_time/end_time to leave_days, drop unique constraint

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-28
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # حذف UniqueConstraint قدیمی
    op.drop_constraint('uq_leave_employee_day', 'leave_days', type_='unique')

    # اضافه کردن ستون‌های جدید
    op.add_column('leave_days', sa.Column('start_time', sa.String(8), nullable=True))
    op.add_column('leave_days', sa.Column('end_time', sa.String(8), nullable=True))

    # ایندکس ترکیبی برای جستجوی سریع‌تر
    op.create_index('ix_leave_days_employee_day', 'leave_days', ['employee_id', 'local_day'])


def downgrade() -> None:
    op.drop_index('ix_leave_days_employee_day', 'leave_days')
    op.drop_column('leave_days', 'end_time')
    op.drop_column('leave_days', 'start_time')
    op.create_unique_constraint('uq_leave_employee_day', 'leave_days', ['employee_id', 'local_day'])
