from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    SQL_QUERY_TIMEOUT: Optional[float] = None
    MAX_FEATURES_PER_TILE: int
    MIN_ZOOM_CLUSTERING: int
    MIN_FEATURE_CNT_CLUSTERING: int


settings = Settings()
