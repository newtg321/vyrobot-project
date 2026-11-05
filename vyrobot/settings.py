"""
Django settings for Vyrobot.

Security-relevant defaults:
- SECRET_KEY comes from the environment in production; the fallback is dev-only.
- DEBUG defaults to False. The dev-only test faucet (see accounts/views.py)
  is gated on DEBUG and must never be enabled on a live deployment.
- Passwords are hashed by Django's default PBKDF2 hasher. Plaintext is never stored.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-only-insecure-key-change-me-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "False").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o
]

# --- Render deployment ------------------------------------------------------
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_render_host}")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "jazzmin",
    "django.contrib.admin",
    "django_extensions",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vyrobot.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "vyrobot.wsgi.application"
ASGI_APPLICATION = "vyrobot.asgi.application"

# Default to SQLite for easy local runs. Swap to PostgreSQL via env for production.
if os.environ.get("DATABASE_URL"):
    # Minimal parse for a postgres URL if you want it; otherwise keep SQLite.
    import dj_database_url  # optional dependency; only needed if DATABASE_URL set

    DATABASES = {"default": dj_database_url.parse(os.environ["DATABASE_URL"])}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "auth"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "auth"

# --- Platform economics (transparent, operator-configurable) --------------
# Staking reward APR, expressed as a fraction per year (0.12 = 12% APR).
# Rewards accrue on staked principal and are CLAIMABLE ONLY up to the amount
# the operator has actually funded into the reward pool (see RewardPool).
# This cap is deliberate: it prevents paying out yield that does not exist.
STAKING_APR = float(os.environ.get("VYROBOT_STAKING_APR", "0.12"))

# Lock-period options (days) the user can choose when staking.
STAKE_DAY_OPTIONS = [14, 21, 28]

# Early-exit (emergency withdraw before unlock): the percent of the staked
# principal the user gets back. The remainder is forfeited, and so is the
# pending reward. Disclosed to the user before they confirm.
EMERGENCY_RETURN_PCT = int(os.environ.get("VYROBOT_EMERGENCY_RETURN_PCT", "60"))

# Coin / network options offered at withdrawal.
WITHDRAW_NETWORKS = [
    "USDT ERC20", "ETH ERC20", "BNB BEP20", "USDT BEP20",
    "TRON TRX20", "USDT TRX20", "Solana", "USDT SOL",
]

# Production security hardening (only applied when DEBUG is off).
# if not DEBUG:
#     SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "True").lower() in ("1", "true", "yes")
#     SESSION_COOKIE_SECURE = True
#     CSRF_COOKIE_SECURE = True
#     SECURE_HSTS_SECONDS = 2592000
#     SECURE_HSTS_INCLUDE_SUBDOMAINS = True
#     SECURE_HSTS_PRELOAD = True
#     SECURE_CONTENT_TYPE_NOSNIFF = True
#     X_FRAME_OPTIONS = "DENY"
