"""A Flask app with one route, called through Flask's test client."""

from flask import Flask, jsonify

app = Flask(__name__)
STOCK = {"KBD-0101": 3}


def lookup(sku):
    try:
        return STOCK[sku]
    except KeyError:
        return 0  # an unknown SKU counts as empty


@app.get("/stock/<sku>")
def stock(sku):
    return jsonify(sku=sku, on_hand=lookup(sku))


if __name__ == "__main__":
    client = app.test_client()
    for sku in ("KBD-0101", "nope"):
        response = client.get(f"/stock/{sku}")
        print(response.status_code, response.get_json())
