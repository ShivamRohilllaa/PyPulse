def navigation(request):
    return {"navigation": [("Traffic", "/"), ("Uptime", "/uptime/"), ("Benchmarks", "/benchmarks/"),
                           ("Incidents", "/incidents/")]}
