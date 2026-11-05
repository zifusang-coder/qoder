import json
from typing import Any

from sqlalchemy import Text, bindparam
from sqlalchemy.types import TypeDecorator

EMBEDDING_DIM = 1024


class _EmbeddingComparator(TypeDecorator.Comparator):
    def cosine_distance(self, other: Any):
        # <=> 为 pgvector 的余弦距离；仅在 PostgreSQL 方言下使用
        from pgvector.sqlalchemy import Vector

        return self.expr.op("<=>")(bindparam(None, other, type_=Vector(EMBEDDING_DIM)))


class EmbeddingVector(TypeDecorator):
    """PostgreSQL 使用 pgvector，其他方言（测试中的 SQLite）退化为 JSON 文本。"""

    impl = Text
    cache_ok = True
    comparator_factory = _EmbeddingComparator

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(EMBEDDING_DIM))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        vector = [float(item) for item in value]
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(f"向量维度必须为 {EMBEDDING_DIM}")
        if dialect.name == "postgresql":
            return vector
        return json.dumps(vector)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            return [float(item) for item in json.loads(value)]
        if hasattr(value, "tolist"):
            value = value.tolist()
        return [float(item) for item in value]
