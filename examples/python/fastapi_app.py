from fastapi import FastAPI
from pypulse_agent.fastapi import PulseMiddleware

app = FastAPI()
app.add_middleware(PulseMiddleware)


@app.get("/orders/{order_id}")
async def order(order_id: int):
    return {"id": order_id, "state": "ready"}
