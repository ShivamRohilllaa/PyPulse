from time import perf_counter

from .agent import Agent


class PulseMiddleware:
    """ASGI middleware for FastAPI. Measures through the final HTTP response body."""
    def __init__(self, app, agent=None):
        self.app = app
        self.agent = agent or Agent.from_env()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = perf_counter()
        status = 500
        finished = False

        def observe(code):
            template = getattr(scope.get("route"), "path", None)
            self.agent.record(scope["method"], template, code, (perf_counter() - started) * 1000)

        async def send_timed(message):
            nonlocal status, finished
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
                observe(status)

        try:
            await self.app(scope, receive, send_timed)
        finally:
            if not finished:
                observe(500)
