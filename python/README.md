# PyPulse Python agent

Install from the checkout with `python -m pip install ./python`. Set `PYPULSE_URL` and the per-service `PYPULSE_TOKEN`, register the framework adapter and restart your workers. [Step-by-step integrations](../README.md#connect-your-framework)

The worker aggregates matched routes, status classes, latency buckets and synchronous Django SQL counts, then sends a batch every ten seconds. It sends no HTTP bodies, client IPs or query strings. Each worker keeps up to 60 batches in memory while the collector is unreachable. Delivery retries reuse the batch ID. The dashboard accepts a batch once per service.

Django and Flask time response creation. FastAPI includes the last ASGI response body. SQL counts cover executions in the synchronous Django request thread. Missing routes share a single unmatched group. Worker memory resets after a fork. Short-lived callers can call `agent.close()` to attempt a final flush.
