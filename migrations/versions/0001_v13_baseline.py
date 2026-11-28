"""v1.3 baseline: the schema v1.3 created with create_all.

An existing v1.3 database already has exactly these objects; adopt it with
`alembic stamp 0001` (never `upgrade`), then `alembic upgrade head`.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        'employees',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_at', TZ, nullable=False),
    )
    op.create_index('ix_employees_device_id', 'employees', ['device_id'], unique=True)
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('username', sa.String(80), nullable=False),
        sa.Column('password_hash', sa.String(250), nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('username'),
    )
    op.create_table(
        'imports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('uploader_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('uploaded_at', TZ, nullable=False),
        sa.Column('accepted', sa.Integer(), nullable=False),
        sa.Column('rejected', sa.Integer(), nullable=False),
        sa.Column('note', sa.String(500), nullable=False),
        sa.UniqueConstraint('digest'),
    )
    op.create_table(
        'report_sets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('import_id', sa.Integer(), sa.ForeignKey('imports.id'), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.UniqueConstraint('import_id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'report_days',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('report_id', sa.Integer(), sa.ForeignKey('report_sets.id'), nullable=False),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.UniqueConstraint('report_id', 'device_id', 'local_day', name='unique_report_employee_day'),
    )
    for col in ('report_id', 'device_id', 'local_day'):
        op.create_index(f'ix_report_days_{col}', 'report_days', [col])
    op.create_table(
        'report_punches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('report_id', sa.Integer(), sa.ForeignKey('report_sets.id'), nullable=False),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('local_day', sa.String(10), nullable=False),
        sa.Column('occurred_at', TZ, nullable=False),
        sa.UniqueConstraint('report_id', 'device_id', 'occurred_at', name='unique_report_punch'),
    )
    for col in ('report_id', 'device_id', 'local_day'):
        op.create_index(f'ix_report_punches_{col}', 'report_punches', [col])
    op.create_table(
        'attendance_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('device_id', sa.String(80), nullable=False),
        sa.Column('occurred_at', TZ, nullable=False),
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
        sa.Column('first_punch', TZ, nullable=False),
        sa.Column('last_punch', TZ, nullable=True),
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
        sa.Column('created_at', TZ, nullable=False),
    )


def downgrade() -> None:
    raise NotImplementedError('the v1.3 baseline is never dropped by a migration')
