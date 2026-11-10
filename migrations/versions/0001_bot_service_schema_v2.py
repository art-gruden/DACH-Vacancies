"""bot service schema v2

Revision ID: 0001
Revises: 
Create Date: 2026-11-10 10:49:09.652822
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bot_users',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('ui_language', sa.String(length=5), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('quiet_start', sa.String(length=5), nullable=True),
    sa.Column('quiet_end', sa.String(length=5), nullable=True),
    sa.Column('daily_limit', sa.Integer(), nullable=False),
    sa.Column('ai_consent_version', sa.Integer(), nullable=True),
    sa.Column('ai_consent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cv_consent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('crawler_event_inbox',
    sa.Column('event_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('fields', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('reason', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.String(length=500), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ready_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_index(op.f('ix_crawler_event_inbox_job_id'), 'crawler_event_inbox', ['job_id'], unique=False)
    op.create_index('ix_inbox_status_event', 'crawler_event_inbox', ['status', 'event_id'], unique=False)
    op.create_table('crawler_sync_cursor',
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('cursor', sa.Text(), nullable=False),
    sa.Column('last_event_id', sa.BigInteger(), nullable=False),
    sa.Column('resyncs', sa.Integer(), nullable=False),
    sa.Column('resync_since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_poll_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('embedding_cache',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('vector', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('facet_cache',
    sa.Column('key', sa.String(length=120), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('job_cache',
    sa.Column('job_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('crawler_updated_at', sa.String(length=40), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('job_id')
    )
    op.create_table('ai_usage',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=True),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('tokens_in', sa.Integer(), nullable=False),
    sa.Column('tokens_out', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('cached', sa.Boolean(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error_kind', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ai_usage_user_id'), 'ai_usage', ['user_id'], unique=False)
    op.create_table('candidate_profiles',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('cv_documents',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('file_sha256', sa.String(length=64), nullable=False),
    sa.Column('mime', sa.String(length=100), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('encrypted_text', sa.LargeBinary(), nullable=True),
    sa.Column('extracted', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('delete_after', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cv_documents_user_id'), 'cv_documents', ['user_id'], unique=False)
    op.create_table('notification_outbox',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('job_id', sa.BigInteger(), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('dedup_key', sa.String(length=120), nullable=False),
    sa.Column('subscription_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.String(length=300), nullable=True),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
    sa.Column('batch_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedup_key')
    )
    op.create_index(op.f('ix_notification_outbox_user_id'), 'notification_outbox', ['user_id'], unique=False)
    op.create_index('ix_outbox_status_due', 'notification_outbox', ['status', 'due_at'], unique=False)
    op.create_table('questionnaire_drafts',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('saved_jobs',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('company', sa.String(length=200), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('job_closed', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'job_id')
    )
    op.create_table('search_sessions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('criteria', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('results', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('shown', sa.Integer(), nullable=False),
    sa.Column('baseline_event_id', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_search_sessions_user_id'), 'search_sessions', ['user_id'], unique=False)
    op.create_table('subscriptions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('criteria', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('profile_version', sa.Integer(), nullable=True),
    sa.Column('notification_mode', sa.String(length=16), nullable=False),
    sa.Column('notify_updates', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('start_after_event_id', sa.BigInteger(), nullable=False),
    sa.Column('watermark_event_id', sa.BigInteger(), nullable=False),
    sa.Column('weights', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_subscriptions_user_id'), 'subscriptions', ['user_id'], unique=False)
    op.create_table('feedback',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('subscription_id', sa.Integer(), nullable=True),
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('response', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['bot_users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_feedback_user_id'), 'feedback', ['user_id'], unique=False)
    op.create_table('subscription_matches',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('subscription_id', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('job_revision', sa.String(length=40), nullable=True),
    sa.Column('score', sa.Integer(), nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('origin', sa.String(length=16), nullable=False),
    sa.Column('notified', sa.Boolean(), nullable=False),
    sa.Column('llm_explanation', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('model_version', sa.String(length=40), nullable=False),
    sa.Column('evaluated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('subscription_id', 'job_id', name='uq_match_sub_job')
    )
    op.create_index(op.f('ix_subscription_matches_job_id'), 'subscription_matches', ['job_id'], unique=False)
    op.create_index(op.f('ix_subscription_matches_subscription_id'), 'subscription_matches', ['subscription_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_subscription_matches_subscription_id'), table_name='subscription_matches')
    op.drop_index(op.f('ix_subscription_matches_job_id'), table_name='subscription_matches')
    op.drop_table('subscription_matches')
    op.drop_index(op.f('ix_feedback_user_id'), table_name='feedback')
    op.drop_table('feedback')
    op.drop_index(op.f('ix_subscriptions_user_id'), table_name='subscriptions')
    op.drop_table('subscriptions')
    op.drop_index(op.f('ix_search_sessions_user_id'), table_name='search_sessions')
    op.drop_table('search_sessions')
    op.drop_table('saved_jobs')
    op.drop_table('questionnaire_drafts')
    op.drop_index('ix_outbox_status_due', table_name='notification_outbox')
    op.drop_index(op.f('ix_notification_outbox_user_id'), table_name='notification_outbox')
    op.drop_table('notification_outbox')
    op.drop_index(op.f('ix_cv_documents_user_id'), table_name='cv_documents')
    op.drop_table('cv_documents')
    op.drop_table('candidate_profiles')
    op.drop_index(op.f('ix_ai_usage_user_id'), table_name='ai_usage')
    op.drop_table('ai_usage')
    op.drop_table('job_cache')
    op.drop_table('facet_cache')
    op.drop_table('embedding_cache')
    op.drop_table('crawler_sync_cursor')
    op.drop_index('ix_inbox_status_event', table_name='crawler_event_inbox')
    op.drop_index(op.f('ix_crawler_event_inbox_job_id'), table_name='crawler_event_inbox')
    op.drop_table('crawler_event_inbox')
    op.drop_table('bot_users')
