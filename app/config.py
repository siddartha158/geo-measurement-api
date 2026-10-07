"""Application settings, read from environment variables with sane defaults."""
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'geo_api.db'}")
    # Maximum size of the uploaded file itself.
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
    # Maximum total size of everything inside an uploaded .zip (zip-bomb protection).
    max_uncompressed_bytes: int = int(os.getenv("MAX_UNCOMPRESSED_MB", "200")) * 1024 * 1024


settings = Settings()
