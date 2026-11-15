# Settings for the standalone DRF example.
SECRET_KEY = "local-example"
ROOT_URLCONF = "examples.python.django_urls"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
INSTALLED_APPS = ["rest_framework"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
REST_FRAMEWORK = {
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}
MIDDLEWARE = ["pypulse_agent.django.middleware"]
