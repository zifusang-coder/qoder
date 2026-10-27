from sqlalchemy import Boolean, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class LLMProvider(TenantScoped):
    __tablename__ = "llm_providers"
    __table_args__ = (
        Index(
            "uq_llm_providers_tenant_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_enc: Mapped[str] = mapped_column(String(1000))
    tiers: Mapped[dict[str, str]] = mapped_column(JSONType, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
