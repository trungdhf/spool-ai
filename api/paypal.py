"""PayPal Orders API (sandbox by default): create an order, then capture it on return."""
import base64
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler

BASE = os.environ.get("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")


def _token():
    cid, sec = os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]
    req = urllib.request.Request(
        BASE + "/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        })
    return json.loads(urllib.request.urlopen(req, 20).read())["access_token"]


def _req(path, tok, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, 30).read())


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            action = body.get("action")
            tok = _token()
            if action == "create":
                ret = body.get("return_url", "https://spool-ai-one.vercel.app/")
                o = _req("/v2/checkout/orders", tok, {
                    "intent": "CAPTURE",
                    "purchase_units": [{
                        "amount": {"currency_code": "USD", "value": str(body.get("amount", "10.00"))},
                        "description": body.get("description", "Spool fabrication order")[:127],
                    }],
                    "application_context": {"return_url": ret, "cancel_url": ret + "?cancelled=1",
                                            "brand_name": "Spool AI", "user_action": "PAY_NOW"},
                })
                approve = next(l["href"] for l in o["links"] if l["rel"] == "approve")
                out = json.dumps({"order_id": o["id"], "approve_url": approve}).encode()
            elif action == "capture":
                c = _req(f"/v2/checkout/orders/{body['order_id']}/capture", tok, {})
                cap = c["purchase_units"][0]["payments"]["captures"][0]
                out = json.dumps({
                    "status": c["status"],
                    "capture_id": cap["id"],
                    "amount": cap["amount"]["value"] + " " + cap["amount"]["currency_code"],
                    "payer": c.get("payer", {}).get("email_address", ""),
                    "description": c["purchase_units"][0].get("description", ""),
                }).encode()
            else:
                out = json.dumps({"error": "action must be create|capture"}).encode()
            code = 200
        except Exception as e:  # noqa: BLE001
            out = json.dumps({"error": str(e)}).encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
