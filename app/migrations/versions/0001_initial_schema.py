"""Initial attendance schema for a NEW, empty database only.

Revision ID: 0001_initial
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'employees',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_employees_device_id', 'employees', ['device_id'], unique=True)

    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('username', sa.String(80), nullable=False, unique=True),
        sa.Column('password_hash', sa.String(250), nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
    )

    op.create_table(
        'imports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False, unique=True),
        sa.Column('uploader_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted', sa.Integer(), nullable=False),
        sa.Column('rejected', sa.Integer(), nullable=False),
        sa.Column('note', sa.String(500), nullable=False),
    )

    op.create_table(
        'report_sets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('import_id', sa.Integer(), sa.ForeignKey('imports.id'), nullable=False, unique=True),
        sa.Column('name', sa.String(120), nullable=False, unique=True),
    )

    op.create_table(
        'report_days',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('report_id', sa.Integer(), sa.ForeignKey('report_sets.id'), nullable=False),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.UniqueConstraint('report_id', 'device_id', 'local_day', name='unique_report_employee_day'),
    )
    op.create_index('ix_report_days_report_id', 'report_days', ['report_id'])
    op.create_index('ix_report_days_device_id', 'report_days', ['device_id'])
    op.create_index('ix_report_days_local_day', 'report_days', ['local_day'])

    op.create_table(
        'report_punches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('report_id', sa.Integer(), sa.ForeignKey('report_sets.id'), nullable=False),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('report_id', 'device_id', 'occurred_at', name='unique_report_punch'),
    )
    op.create_index('ix_report_punches_report_id', 'report_punches', ['report_id'])
    op.create_index('ix_report_punches_device_id', 'report_punches', ['device_id'])
    op.create_index('ix_report_punches_local_day', 'report_punches', ['local_day'])

    op.create_table(
        'attendance_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_import_id', sa.Integer(), sa.ForeignKey('imports.id'), nullable=False),
        sa.UniqueConstraint('device_id', 'occurred_at', name='unique_device_punch'),
    )
    op.create_index('ix_attendance_logs_device_id', 'attendance_logs', ['device_id'])
    op.create_index('ix_attendance_logs_occurred_at', 'attendance_logs', ['occurred_at'])

    op.create_table(
        'attendance_records',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.Column('first_punch', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_punch', sa.DateTime(timezone=True), nullable=True),
        sa.Column('punch_count', sa.Integer(), nullable=False),
        sa.UniqueConstraint('device_id', 'local_day', name='unique_attendance_day'),
    )
    op.create_index('ix_attendance_records_device_id', 'attendance_records', ['device_id'])
    op.create_index('ix_attendance_records_local_day', 'attendance_records', ['local_day'])

    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('actor', sa.String(80), nullable=False),
        sa.Column('event', sa.String(80), nullable=False),
        sa.Column('details', sa.String(300), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    # Explicitly refuse destructive rollback; restore through an approved backup plan.
    raise RuntimeError('Initial schema downgrade is destructive and intentionally disabled.')
