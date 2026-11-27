import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class ApiKey(TenantScoped):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    # 只存 sha256 摘要；明文密钥只在创建响应里出现一次。
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list] = mapped_column(JSONType, default=list)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
