import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import httpx
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

@pytest.fixture(scope="session")
def dashboard(tmp_path_factory):
    directory=tmp_path_factory.mktemp("collector")
    password=secrets.token_urlsafe(24)
    env=dict(os.environ,PYPULSE_DATA_DIR=str(directory),DJANGO_SETTINGS_MODULE="pulse.settings",
             DJANGO_SUPERUSER_USERNAME="tester",DJANGO_SUPERUSER_PASSWORD=password,DJANGO_SUPERUSER_EMAIL="tester@example.test")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    for args in [["migrate","--noinput"],["createsuperuser","--noinput"]]:
        subprocess.run([sys.executable,"manage.py",*args],cwd=ROOT,env=env,check=True,capture_output=True)
    with (directory/"server.log").open("w") as log:
        proc=subprocess.Popen([sys.executable,"manage.py","runserver",f"127.0.0.1:{port}","--noreload"],cwd=ROOT,env=env,stdout=log,stderr=log)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}",trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get("/healthz").is_success:break
                    except httpx.ConnectError:pass
                    time.sleep(.1)
                client.get("/login/")
                response=client.post("/login/",data={"username":"tester","password":password,"csrfmiddlewaretoken":client.cookies["csrftoken"]})
                assert response.status_code==302
                client.headers["X-CSRFToken"]=client.cookies["csrftoken"]
                yield client
        finally:
            proc.terminate()
            proc.wait(timeout=8)

@pytest.fixture
def service(dashboard):
    from pypulse_agent import Agent
    agents=[]
    def create(framework):
        r=dashboard.post("/api/python/services",json={"name":framework+secrets.token_hex(3),"framework":framework,"target":99})
        r.raise_for_status()
        data=r.json()
        agent=Agent(str(dashboard.base_url),data["token"],interval=60)
        agents.append(agent)
        return data["service"]["id"],agent
    yield create
    for agent in agents: agent.close()
