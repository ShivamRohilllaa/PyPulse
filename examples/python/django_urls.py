from time import sleep

from django.db import connection
from django.urls import path
from rest_framework.decorators import api_view
from rest_framework.response import Response


@api_view(["GET"])
def order(request, order_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s", [order_id])
        value, = cursor.fetchone()
    return Response({"id": value, "state": "ready"})


@api_view(["GET"])
def slow(request):
    sleep(0.32)
    return Response({"state": "ready", "demo_delay_ms": 320})


@api_view(["GET"])
def error(request):
    return Response({"detail": "Intentional demo error"}, status=500)


urlpatterns = [
    path("orders/<int:order_id>", order),
    path("demo/slow", slow),
    path("demo/error", error),
]
