"""Add profiles table to mirror Supabase auth.users

Revision ID: 20260805_add_profiles_table
Revises: 
Create Date: 2026-08-05 19:58:57.036
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20260805_add_profiles_table'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Create profiles table
    op.create_table(
        'profiles',
        sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('username', sa.Text(), nullable=True),
        sa.Column('full_name', sa.Text(), nullable=True),
        sa.Column('avatar_url', sa.Text(), nullable=True),
        sa.Column('website', sa.Text(), nullable=True),
    )

    # Unique constraint on username
    op.create_unique_constraint('uq_profiles_username', 'profiles', ['username'])

    # CHECK constraint: username length >= 3 (applies when username is not null)
    op.create_check_constraint('profiles_username_length_check', 'profiles', "char_length(username) >= 3")

    # Foreign key to auth.users(id) (Supabase auth schema)
    # Use referent_schema to point to the auth schema.
    op.create_foreign_key(
        'profiles_id_fkey',
        source_table='profiles',
        referent_table='users',
        local_cols=['id'],
        remote_cols=['id'],
        referent_schema='auth',
        ondelete=None,
    )


def downgrade():
    # Drop foreign key
    try:
        op.drop_constraint('profiles_id_fkey', 'profiles', type_='foreignkey')
    except Exception:
        pass

    # Drop check constraint
    try:
        op.drop_constraint('profiles_username_length_check', 'profiles', type_='check')
    except Exception:
        pass

    # Drop unique constraint
    try:
        op.drop_constraint('uq_profiles_username', 'profiles', type_='unique')
    except Exception:
        pass

    # Drop table
    op.drop_table('profiles')
