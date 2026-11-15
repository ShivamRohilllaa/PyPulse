"""One-time local import for the initial PyPulse SQLite workspace."""
from datetime import datetime,timezone
import csv
import gzip
import json
from pathlib import Path
import sqlite3
import uuid
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from console.models import Batch, Service, Slice, Run, Exercise

def date(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc) if ms else None

class Command(BaseCommand):
    help="Import local accounts, service tokens, route counts and completed fixed GET demo results into an empty workspace."
    def add_arguments(self,parser):
        parser.add_argument("source",type=Path)
    @transaction.atomic
    def handle(self,source,**kwargs):
        User=get_user_model()
        if User.objects.exists() or Service.objects.exists() or Exercise.objects.exists():
            raise CommandError("Import into an empty, migrated workspace.")
        db=sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True)
        db.row_factory=sqlite3.Row
        try:
            for u in db.execute("SELECT * FROM users"):
                User.objects.create(username=u["email"],email=u["email"],first_name=u["name"] or "",is_active=not u["disabled"],
                    is_staff=u["role"]=="admin",is_superuser=u["role"]=="admin",password="bcrypt$"+u["password_hash"])
            for s in db.execute("SELECT * FROM python_services"):
                Service.objects.create(pk=s["id"],name=s["name"],framework=s["framework"],target=s["target"],
                    token_hash=s["token_hash"],last_seen=date(s["last_seen"]))
            for r in db.execute("SELECT * FROM python_minutes"):
                key=uuid.uuid5(uuid.NAMESPACE_OID,f"legacy/{r['service']}/{r['minute']}")
                b,_=Batch.objects.get_or_create(service_id=r["service"],batch_id=key,defaults={"at":date(r["minute"])})
                Slice.objects.create(batch=b,method=r["method"],route=r["route"],count=r["count"],
                    client_errors=r["clientErrors"],server_errors=r["serverErrors"],sql_samples=r["sqlSamples"],
                    sql_queries=r["sqlQueries"],histogram=[r[f"b{i}"] for i in range(9)])
            for r in db.execute("SELECT * FROM test_runs WHERE status='completed' AND kind='load'"):
                cfg=json.loads(r["config"])
                steps=cfg.get("steps") or []
                if cfg.get("mode")!="fixed" or len(steps)!=1 or steps[0]["request"].get("method")!="GET":
                    continue
                ex=Exercise.objects.create(name=r["name"],url=steps[0]["request"]["url"],rate=cfg.get("rps",10),seconds=cfg.get("durationSec",30))
                s=json.loads(r["summary"]); lat=s["latency"]
                result={"count":s["totalRequests"],"errors":s["totalErrors"],"codes":s["statusCodes"],"rps":s["throughput"]["avg"],
                     "p50":lat["p50"],"p95":lat["p95"],"p99":lat["p99"],"dropped":s["droppedIterations"],"error_pct":s["errorRate"]}
                history=json.loads(r["timeseries"] or "[]")
                series=[{"second":i+1,"count":point.get("rps",0),"p95":point.get("p95"),"errors":point.get("errors",0)} for i,point in enumerate(history)]
                raw=source.parent/"runs"/f"run-{r['id']}.csv.gz"
                samples=[]
                if raw.is_file():
                    with gzip.open(raw,"rt") as f:
                        for row in csv.DictReader(f):
                            samples.append([float(row["elapsed_ms"])/1000,float(row["latency_ms"]),int(row["status"] or 0)])
                Run.objects.create(pk=r["id"],exercise=ex,state="Completed",started=date(r["started_at"]),finished=date(r["finished_at"]),
                    config={"url":ex.url,"pattern":"fixed","rate":ex.rate,"seconds":ex.seconds,"max_p95":0,"max_error_pct":0},
                    result=result,series=series,samples=samples)
        finally:
            db.close()
        self.stdout.write("Imported accounts, connected services, route aggregates and supported runs.")
