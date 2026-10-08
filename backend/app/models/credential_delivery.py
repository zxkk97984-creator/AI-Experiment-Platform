"""Short-lived encrypted delivery of independently generated student credentials."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class CredentialDelivery(Base):
    __tablename__ = "credential_deliveries"
    __table_args__ = (
        UniqueConstraint("batch_id", "recipient_key", name="uq_credential_delivery_recipient"),
        CheckConstraint("state IN ('prepared', 'issued', 'cleared')", name="ck_credential_delivery_state"),
        Index("ix_credential_delivery_user", "user_id", "state"),
        Index("ix_credential_delivery_expiry", "state", "export_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("admin_batches.id", ondelete="SET NULL"), nullable=True, index=True)
    recipient_key: Mapped[str] = mapped_column(String(100))
    ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="prepared", server_default="prepared")
    issued_credential_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    export_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
