"""A FastAPI app with a sync and an async route, called through its test client."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()
STOCK = {"KBD-0101": 3}


def lookup(sku):
    try:
        return STOCK[sku]
    except KeyError:
        return 0  # an unknown SKU counts as empty


@app.get("/stock/{sku}")
def stock(sku: str):
    return {"sku": sku, "on_hand": lookup(sku)}


@app.get("/slow/{sku}")
async def slow(sku: str):
    await asyncio.sleep(0)
    return {"sku": sku, "on_hand": lookup(sku)}


if __name__ == "__main__":
    with TestClient(app) as client:
        for path in ("/stock/KBD-0101", "/stock/nope", "/slow/nope"):
            response = client.get(path)
            print(response.status_code, response.json())
