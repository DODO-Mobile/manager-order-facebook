import datetime as dt
from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer,
                        String, Text, UniqueConstraint, create_engine, Index, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class FacebookPost(Base):
    __tablename__ = "facebook_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[str] = mapped_column(String(64))
    post_id: Mapped[str] = mapped_column(String(128))
    campaign_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    catalog_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # per-post message template override (str.format style), optional
    message_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("page_id", "post_id", name="uq_post"),)


class PostInfo(Base):
    """Post content fetched from Graph via webhook post_id (auto, no manual config)."""
    __tablename__ = "post_info"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[str] = mapped_column(String(64))
    post_id: Mapped[str] = mapped_column(String(128))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    permalink_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("page_id", "post_id", name="uq_post_info"),)


class WebhookEvent(Base):
    """Every POST hit on the webhook (valid or not) for debugging."""
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    signature_ok: Mapped[bool] = mapped_column(Boolean)
    comment_events: Mapped[int] = mapped_column(Integer, default=0)
    remote: Mapped[str | None] = mapped_column(String(64), nullable=True)
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)        # raw body, capped 100KB
    outcomes: Mapped[list | None] = mapped_column(JSON, nullable=True)   # per comment event: queued|duplicate


class CatalogItem(Base):
    """Product variant source of truth for price. Prices never come from AI."""
    __tablename__ = "catalog_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_id: Mapped[str] = mapped_column(String(64), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    unit_price: Mapped[int] = mapped_column(BigInteger)
    colors: Mapped[dict] = mapped_column(JSON, default=dict)   # {"xanh": ["xanh","xanh duong"]}
    sizes: Mapped[list] = mapped_column(JSON, default=list)    # ["XS","S"]
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("catalog_id", "sku", name="uq_catalog_sku"),)


class FacebookComment(Base):
    __tablename__ = "facebook_comments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[str] = mapped_column(String(64))
    comment_id: Mapped[str] = mapped_column(String(128))
    post_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("page_id", "comment_id", name="uq_comment"),)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[str] = mapped_column(String(64))
    comment_id: Mapped[str] = mapped_column(String(128))
    items_json: Mapped[list] = mapped_column(JSON, default=list)
    total: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="VND")
    status: Mapped[str] = mapped_column(String(32))  # READY | NEEDS_REVIEW
    review_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parser_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # one order per comment
    __table_args__ = (UniqueConstraint("page_id", "comment_id", name="uq_order_comment"),)


class MessageLog(Base):
    """Idempotency key: (page_id, comment_id, action)."""
    __tablename__ = "message_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[str] = mapped_column(String(64))
    comment_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(32), default="private_reply")
    channel: Mapped[str] = mapped_column(String(32), default="messenger")
    # PENDING | SENDING | SENT | FAILED | UNKNOWN
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    meta_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_subcode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("page_id", "comment_id", "action", name="uq_message_action"),)


class FacebookCustomer(Base):
    __tablename__ = "facebook_customers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[str] = mapped_column(String(64))
    identity_key: Mapped[str] = mapped_column(String(160))
    facebook_user_id: Mapped[str | None] = mapped_column(String(64))
    psid: Mapped[str | None] = mapped_column(String(64))
    facebook_name: Mapped[str | None] = mapped_column(Text)
    profile_picture: Mapped[str | None] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(254))
    source: Mapped[str] = mapped_column(String(16))
    collected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("page_id", "identity_key", name="uq_customer_identity"),)


class MessengerOrderCustomer(Base):
    __tablename__ = "messenger_order_customers"
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("facebook_customers.id"), primary_key=True)


class Job(Base):
    """Durable queue. Webhook inserts; worker claims with SKIP LOCKED."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)  # page:comment
    correlation_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    # PENDING | RUNNING | DONE | DEAD
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_jobs_pick", "status", "next_run_at"),)


def create_schema(engine):
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # API and worker can initialize the same database concurrently.
            connection.execute(text("SELECT pg_advisory_xact_lock(264318, 1)"))
        Base.metadata.create_all(connection)


def make_session_factory(url: str):
    kwargs = {"pool_pre_ping": True, "hide_parameters": True}
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}}
    engine = create_engine(url, **kwargs)
    return engine, sessionmaker(engine, expire_on_commit=False)
