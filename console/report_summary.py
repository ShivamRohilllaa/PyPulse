"""Explain recorded observations without guessing service capacity or exposing request values."""
from collections import Counter


def explain(run):
    metrics = run.result
    count = metrics.get("count", 0)
    errors = metrics.get("errors", 0)
    passed = metrics.get("passed")
    if run.finished is None:
        return None
    headline = f"{count:,} requests finished with {errors:,} errors."
    if not count:
        headline = "This run produced no measured responses."
    elif run.state != "Completed":
        headline = f"{run.state} after {count:,} requests."
    elif passed is False:
        headline += " The release gate failed."
    elif passed is True:
        headline += " The release gate passed."
    findings, actions = [], []
    failures = metrics.get("failures") or {}
    for reason, frequency in Counter(failures).most_common(3):
        findings.append(f"{frequency:,} requests recorded “{reason}”.")
    codes = metrics.get("codes") or {}
    if errors and any(f"HTTP {code}" in failures or (not failures and str(code) in codes) for code in (401, 403)):
        actions.append("Check the test credentials and permissions for the failing step.")
    if errors and ("HTTP 429" in failures or (not failures and "429" in codes)):
        actions.append("Compare the arrival rate with the target’s documented rate limit and inspect its retry headers.")
    if errors and any(f"HTTP {code}" in failures or (not failures and str(code) in codes) for code in range(500, 600)):
        actions.append("Match the failed step and run time to application and reverse proxy logs.")
    if metrics.get("dropped") or metrics.get("generator_warning"):
        actions.append("Inspect generator health and dropped iterations before using throughput as a server capacity estimate.")
    if metrics.get("breakpoint"):
        findings.append("The stress test reached a configured stop condition. Review its stage results.")
    p95, median = metrics.get("p95"), metrics.get("p50")
    if p95 is not None:
        findings.append(f"95% of measured durations were at or below {p95:g} ms.")
    if median and p95 and p95 > max(3 * median, median + 100):
        actions.append("Inspect the slowest step and compare its tail latency with the same step in an earlier run.")
    profiles = [p for p in metrics.get("profiles", []) if p.get("p95") is not None]
    if len(profiles) >= 2:
        fastest, slowest = min(profiles, key=lambda x: x["p95"]), max(profiles, key=lambda x: x["p95"])
        if fastest["p95"] < slowest["p95"]:
            findings.append(f"p95 ranged from {fastest['p95']:g} ms on {fastest['label']} to {slowest['p95']:g} ms on {slowest['label']}.")
    phases = metrics.get("phases") or {}
    labels = {"dns": ("DNS lookup", "Review resolver latency from the worker’s network."),
              "tcp": ("TCP connection setup", "Check connection reuse and repeat from the same client network."),
              "tls": ("TLS negotiation", "Compare fresh and reused HTTPS connections."),
              "ttfb": ("waiting for the first byte", "Compare first-byte timing with the application route and database metrics."),
              "download": ("response download", "Check response size and compare a compressed response on the same profile.")}
    measured = {key: value for key, value in phases.items() if key in labels and value is not None and value > 0}
    if measured:
        key = max(measured, key=measured.get)
        label, action = labels[key]
        findings.append(f"The largest average measured HTTP phase was {label} at {measured[key]:g} ms.")
        if measured[key] >= 100:
            actions.append(action)
    for reason in metrics.get("gate_reasons", []):
        if reason not in findings:
            findings.append(reason)
    if not count:
        actions.append("Check the worker status, target configuration and run error before retrying.")
    elif errors and not actions:
        actions.append("Filter the timeline to failed samples and verify the affected step’s expected response.")
    if not actions and passed is False:
        actions.append("Review the failed criteria against individual step and stage measurements.")
    return {"headline": headline, "findings": findings, "actions": actions}


def text_report(run, summary):
    parts = [f"{run.display_name} · Run #{run.pk}", summary["headline"], *summary["findings"]]
    if summary["actions"]:
        parts.append("Recommended checks")
        parts.extend(f"{i}. {action}" for i, action in enumerate(summary["actions"], 1))
    return "\n".join(parts) + "\n"
