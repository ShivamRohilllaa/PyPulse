from time import perf_counter

from flask import g, request, request_finished, request_started, request_tearing_down

from .agent import Agent


def instrument(app, agent=None):
    """Measure dispatch, including Flask error handling, up to response creation."""
    if "pypulse" in app.extensions:
        return app.extensions["pypulse"]
    agent = agent or Agent.from_env()
    app.extensions["pypulse"] = agent

    def start(sender, **kwargs):
        g._pypulse_started = perf_counter()

    def finish(status):
        started = g.pop("_pypulse_started", None)
        if started is not None:
            template = request.url_rule.rule if request.url_rule else None
            agent.record(request.method, template, status, (perf_counter() - started) * 1000)

    def response_done(sender, response, **kwargs):
        finish(response.status_code)

    def teardown(sender, **kwargs):
        finish(500)

    request_started.connect(start, app, weak=False)
    request_finished.connect(response_done, app, weak=False)
    request_tearing_down.connect(teardown, app, weak=False)
    return agent
