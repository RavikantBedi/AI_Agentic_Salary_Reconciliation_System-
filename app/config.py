"""Application configuration.

Everything is environment-overridable so the same code runs on a laptop
(SQLite, single process) and on a server (PostgreSQL, multiple workers).
A ``.env`` file in the project root is read if present -- no extra dependency,
just a small parser, so a non-technical operator can edit one plain text file.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Existing environment variables always win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _secret_key() -> str:
    """Return the session signing key.

    In production this MUST be supplied via ``SECRET_KEY``. For local first
    runs we persist a generated key to ``data/.secret_key`` so that sessions
    survive a restart -- regenerating it on every boot would silently log
    everyone out and look like a bug.
    """
    env = os.getenv("SECRET_KEY")
    if env:
        return env
    key_file = BASE_DIR / "data" / ".secret_key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    generated = secrets.token_urlsafe(48)
    key_file.write_text(generated, encoding="utf-8")
    try:  # best effort: restrict permissions where the OS supports it
        os.chmod(key_file, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return generated


@dataclass
class Settings:
    # --- Core ---
    app_name: str = "Salary & Payment Reconciliation System"
    app_short_name: str = "SalaryRecon"
    environment: str = field(default_factory=lambda: os.getenv("ENVIRONMENT", "development"))
    debug: bool = field(default_factory=lambda: _bool("DEBUG", True))

    # --- Database ---
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'data' / 'reconciliation.db').as_posix()}"
        )
    )
    sql_echo: bool = field(default_factory=lambda: _bool("SQL_ECHO", False))

    # --- Security ---
    secret_key: str = field(default_factory=_secret_key)
    session_cookie: str = "srs_session"
    session_max_age: int = field(default_factory=lambda: _int("SESSION_MAX_AGE", 60 * 60 * 8))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))
    max_login_attempts: int = field(default_factory=lambda: _int("MAX_LOGIN_ATTEMPTS", 6))
    lockout_minutes: int = field(default_factory=lambda: _int("LOCKOUT_MINUTES", 15))
    # Field-level encryption key for bank account numbers (derived from secret
    # key if not separately provided).
    encryption_key: str = field(default_factory=lambda: os.getenv("ENCRYPTION_KEY", ""))

    # --- Rate limiting (spec s50) ---
    rate_limit_window_seconds: int = field(default_factory=lambda: _int("RATE_LIMIT_WINDOW", 60))
    rate_limit_max_requests: int = field(default_factory=lambda: _int("RATE_LIMIT_MAX", 300))
    rate_limit_login_max: int = field(default_factory=lambda: _int("RATE_LIMIT_LOGIN_MAX", 10))
    rate_limit_upload_max: int = field(default_factory=lambda: _int("RATE_LIMIT_UPLOAD_MAX", 40))

    # --- Uploads (spec s50, s95) ---
    storage_dir: Path = field(default_factory=lambda: Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "data" / "documents"))))
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 25))
    allowed_upload_extensions: tuple[str, ...] = (
        ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff",
        ".csv", ".xlsx", ".xls", ".txt",
    )

    # --- Localisation ---
    default_locale: str = field(default_factory=lambda: os.getenv("DEFAULT_LOCALE", "en"))
    default_currency: str = field(default_factory=lambda: os.getenv("DEFAULT_CURRENCY", "INR"))
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Kolkata"))

    # --- Reconciliation tolerances ---
    #: Differences at or below this are treated as exact matches. Bank rounding
    #: and paise-level noise should not manufacture discrepancies.
    match_tolerance_paise: int = field(default_factory=lambda: _int("MATCH_TOLERANCE_PAISE", 100))
    #: A payment later than this many days past the expected date is "delayed".
    delay_threshold_days: int = field(default_factory=lambda: _int("DELAY_THRESHOLD_DAYS", 7))
    #: Auto-confirm allocations at or above this match score (spec s99).
    auto_allocate_score: int = field(default_factory=lambda: _int("AUTO_ALLOCATE_SCORE", 85))
    #: Below this score nothing is auto-posted; a human must decide.
    review_allocate_score: int = field(default_factory=lambda: _int("REVIEW_ALLOCATE_SCORE", 55))

    # --- Optional AI reasoning layer ---
    #: The platform is fully functional with NO API key. Deterministic engines
    #: produce every number; the LLM only rephrases grounded facts and parses
    #: natural-language questions into structured queries.
    #:
    #: Several providers are supported so the narrative layer can run on a free
    #: tier. ``LLM_PROVIDER`` may name one explicitly; left as "auto" the first
    #: provider with a configured key is used, preferring the free ones.
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "auto").strip().lower())

    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "").strip())
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", "").strip())
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip())
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", "").strip())
    ollama_host: str = field(
        default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/"))

    #: Empty means "use the provider's default model" (see agents/llm.py).
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "").strip())
    llm_enabled: bool = field(default_factory=lambda: _bool("LLM_ENABLED", True))
    llm_timeout_seconds: int = field(default_factory=lambda: _int("LLM_TIMEOUT", 45))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 1600))

    # --- Background jobs ---
    worker_threads: int = field(default_factory=lambda: _int("WORKER_THREADS", 2))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    #: Provider -> the setting holding its credential. Ollama needs none.
    #: Free tiers come first so "auto" prefers a provider that costs nothing.
    LLM_KEY_ORDER = (
        ("groq", "groq_api_key"),
        ("gemini", "gemini_api_key"),
        ("openrouter", "openrouter_api_key"),
        ("anthropic", "anthropic_api_key"),
    )

    def resolve_llm_provider(self) -> str:
        """Which provider will actually be used, or "" if none is configured."""
        if not self.llm_enabled:
            return ""

        choice = self.llm_provider
        if choice and choice not in {"auto", "none"}:
            # An explicit choice is honoured even if the key is missing, so the
            # failure is reported as "no key for groq" rather than silently
            # falling through to a different provider.
            return choice

        if choice == "none":
            return ""

        for name, attribute in self.LLM_KEY_ORDER:
            if getattr(self, attribute, ""):
                return name
        return ""

    def llm_key_for(self, provider: str) -> str:
        for name, attribute in self.LLM_KEY_ORDER:
            if name == provider:
                return getattr(self, attribute, "")
        return ""

    @property
    def llm_available(self) -> bool:
        """True when a provider is configured and usable."""
        provider = self.resolve_llm_provider()
        if not provider:
            return False
        if provider == "ollama":
            return True          # local; needs no credential
        return bool(self.llm_key_for(provider))

    def ensure_dirs(self) -> None:
        (BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "data" / "exports").mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
