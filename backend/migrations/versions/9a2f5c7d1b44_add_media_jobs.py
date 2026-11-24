"""add media jobs

Revision ID: 9a2f5c7d1b44
Revises: 4c7b9e2a1d63
Create Date: 2026-11-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9a2f5c7d1b44'
down_revision: Union[str, Sequence[str], None] = '4c7b9e2a1d63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('media_jobs',
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('skill_id', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('content_id', sa.Uuid(), nullable=True),
    sa.Column('skill_run_id', sa.String(length=36), nullable=True),
    sa.Column('error_code', sa.String(length=100), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['content_id'], ['contents.id'], name=op.f('fk_media_jobs_content_id_contents'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_media_jobs_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_media_jobs'))
    )
    op.create_index(op.f('ix_media_jobs_project_id'), 'media_jobs', ['project_id'], unique=False)
    op.create_index(op.f('ix_media_jobs_status'), 'media_jobs', ['status'], unique=False)
    op.create_index(op.f('ix_media_jobs_tenant_id'), 'media_jobs', ['tenant_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_media_jobs_tenant_id'), table_name='media_jobs')
    op.drop_index(op.f('ix_media_jobs_status'), table_name='media_jobs')
    op.drop_index(op.f('ix_media_jobs_project_id'), table_name='media_jobs')
    op.drop_table('media_jobs')
