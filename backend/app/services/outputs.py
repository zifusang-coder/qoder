import uuid
from pathlib import Path

from app.config import get_settings


def adhoc_output_dir(tenant_id: str) -> Path:
    return get_settings().outputs_dir / "adhoc" / tenant_id / uuid.uuid4().hex
