from contextlib import ExitStack
from time import perf_counter

from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.db import connections
from django.utils.decorators import sync_and_async_middleware

from .agent import Agent


@sync_and_async_middleware
def middleware(get_response):
    agent = getattr(settings, "PYPULSE_AGENT", None) or Agent.from_env()

    def record(request, response, started, queries=None):
        match = getattr(request, "resolver_match", None)
        route = getattr(match, "route", None)
        template = "/" + route.lstrip("/") if route is not None else None
        status = response.status_code if response is not None else 500
        agent.record(request.method, template, status, (perf_counter() - started) * 1000, queries)

    if iscoroutinefunction(get_response):
        async def async_call(request):
            started = perf_counter()
            response = None
            try:
                response = await get_response(request)
                return response
            finally:
                record(request, response, started)
        return async_call

    def sync_call(request):
        started = perf_counter()
        queries = 0
        response = None

        def count_query(execute, sql, params, many, context):
            nonlocal queries
            queries += 1
            return execute(sql, params, many, context)

        try:
            with ExitStack() as stack:
                for alias in connections:
                    stack.enter_context(connections[alias].execute_wrapper(count_query))
                response = get_response(request)
                return response
        finally:
            record(request, response, started, queries)

    return sync_call
