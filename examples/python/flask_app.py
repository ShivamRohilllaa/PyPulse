from flask import Flask
from pypulse_agent.flask import instrument

app = Flask(__name__)
instrument(app)


@app.get("/orders/<int:order_id>")
def order(order_id):
    return {"id": order_id, "state": "ready"}
