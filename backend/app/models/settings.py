from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class LLMProvider(TenantScoped):
    __tablename__ = "llm_providers"

    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_enc: Mapped[str] = mapped_column(String(1000))
    tiers: Mapped[dict[str, str]] = mapped_column(JSONType, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
