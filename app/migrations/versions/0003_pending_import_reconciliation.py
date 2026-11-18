"""Add inert, auditable DAT staging and manual conflict decisions.

Revision ID: 0003_reconciliation
Revises: 0002_corrections

ADDITIVE ONLY: existing punches, imports, corrections and reports stay intact.
"""

from alembic import op
import sqlalchemy as sa


revision = '0003_reconciliation'
down_revision = '0002_corrections'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'pending_imports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('report_name', sa.String(120), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('uploader_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('committed_import_id', sa.Integer(), sa.ForeignKey('imports.id', ondelete='SET NULL')),
    )
    op.create_index('ix_pending_imports_status_created', 'pending_imports',
                    ['status', 'created_at'])
    op.create_table(
        'pending_import_rows',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('pending_import_id', sa.Integer(), sa.ForeignKey('pending_imports.id'), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('raw_line', sa.String(2048), nullable=False),
        sa.Column('source_device_id', sa.String(80)),
        sa.Column('occurred_at', sa.DateTime(timezone=True)),
        sa.Column('issue', sa.String(40), nullable=False),
        sa.Column('decision', sa.String(16), nullable=False),
        sa.Column('target_employee_id', sa.Integer(), sa.ForeignKey('employees.id')),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('resolver_id', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('resolved_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('pending_import_id', 'line_number', name='unique_pending_source_line'),
    )
    op.create_index('ix_pending_import_rows_batch_decision', 'pending_import_rows',
                    ['pending_import_id', 'decision'])


def downgrade():
    # Staging includes the original source and immutable adjudication history.
    raise RuntimeError('Staging and reconciliation history must not be destroyed during rollback.')
