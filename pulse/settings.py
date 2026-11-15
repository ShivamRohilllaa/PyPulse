import os
from pathlib import Path
from django.core.management.utils import get_random_secret_key

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("PYPULSE_DATA_DIR", BASE / "data"))
DATA.mkdir(mode=0o700, parents=True, exist_ok=True)

def signing_key():
    supplied = os.environ.get("PYPULSE_SECRET_KEY")
    if supplied:
        return supplied
    path = DATA / "signing.key"
    from tempfile import NamedTemporaryFile
    if not path.exists():
        with NamedTemporaryFile(mode="w", dir=DATA, delete=False) as f:
            f.write(get_random_secret_key())
        try:
            os.link(f.name, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(f.name)
    return path.read_text().strip()

SECRET_KEY = signing_key()
DEBUG = False
ALLOWED_HOSTS = os.environ.get("PYPULSE_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
ROOT_URLCONF = "pulse.urls"
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
                  "django.contrib.messages", "django.contrib.staticfiles", "django.contrib.admin", "console"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware","whitenoise.middleware.WhiteNoiseMiddleware","django.contrib.sessions.middleware.SessionMiddleware",
              "django.middleware.common.CommonMiddleware","django.middleware.csrf.CsrfViewMiddleware",
              "django.contrib.auth.middleware.AuthenticationMiddleware","django.contrib.messages.middleware.MessageMiddleware",
              "django.middleware.clickjacking.XFrameOptionsMiddleware"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3","NAME": DATA / "pypulse.sqlite3",
                         "OPTIONS": {"timeout": 20,"transaction_mode":"IMMEDIATE","init_command":"PRAGMA journal_mode=WAL;"}}}
TEMPLATES = [{"BACKEND":"django.template.backends.django.DjangoTemplates","APP_DIRS":True,
              "OPTIONS":{"context_processors":["django.template.context_processors.request","django.contrib.auth.context_processors.auth","django.contrib.messages.context_processors.messages","console.context.navigation"]}}]
STATIC_URL = "/static/"
STATIC_ROOT = DATA / "static"
WSGI_APPLICATION = "pulse.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if os.environ.get("PYPULSE_TRUST_PROXY") == "1" else None
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE = os.environ.get("PYPULSE_HTTPS") == "1"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher", "django.contrib.auth.hashers.BCryptPasswordHasher"]
DATA_UPLOAD_MAX_MEMORY_SIZE = 1048576
TEST_HOSTS = {h.strip().lower() for h in os.environ.get("PYPULSE_TEST_HOSTS","localhost:8000,127.0.0.1:8000").split(",") if h.strip()}
