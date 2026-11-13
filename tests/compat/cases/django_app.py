"""A Django project in one file: a model, a view, the test client, SQLite in memory."""

import django
from django.conf import settings

settings.configure(
    DEBUG=False,
    SECRET_KEY="not-used",
    ROOT_URLCONF=__name__,
    ALLOWED_HOSTS=["testserver"],
    INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    DEFAULT_AUTO_FIELD="django.db.models.AutoField",
)
django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.http import JsonResponse  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import path  # noqa: E402


def find(name):
    try:
        return User.objects.get(username=name).id
    except User.DoesNotExist:
        return None  # an unknown user is not an error here


def user(request, name):
    return JsonResponse({"name": name, "id": find(name)})


urlpatterns = [path("user/<str:name>", user)]

if __name__ == "__main__":
    call_command("migrate", verbosity=0)
    User.objects.create(username="ana")
    client = Client()
    for name in ("ana", "nobody"):
        response = client.get(f"/user/{name}")
        print(response.status_code, response.json())
