"""Adapters to your real systems. The agent only sees these small interfaces.

OrderSystem must return dicts shaped like:
  {id, customer_email, item, amount, status, days_late, refunded, reshipped}
status in: processing | in_transit | delivered | cancelled | refunded
"""
import json, os, re, urllib.error, urllib.parse, urllib.request
from datetime import date

from .db import Database, tokens


class LocalOrders:
    """Orders stored in our own database (good for pilots; swap for HttpOrders in production)."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, oid):
        r = self.db.one("SELECT o.*, c.email FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.id=?", (oid,))
        if not r:
            return None
        late = 0
        if r["status"] == "in_transit" and r["expected_date"]:
            late = max(0, (date.today() - date.fromisoformat(r["expected_date"])).days)
        return {"id": r["id"], "customer_email": r["email"], "item": r["item"], "amount": r["amount"],
                "status": r["status"], "days_late": late, "refunded": bool(r["refunded"]),
                "reshipped": bool(r["reshipped"])}

    def refund(self, oid):
        o = self.get(oid)
        self.db.x("UPDATE orders SET refunded=1, status='refunded' WHERE id=?", (oid,))
        return {"ok": True, "refunded_amount": o["amount"], "eta": "3-5 business days"}

    def reship(self, oid):
        new_eta = date.fromordinal(date.today().toordinal() + 2).isoformat()
        self.db.x("UPDATE orders SET reshipped=1, expected_date=? WHERE id=?", (new_eta, oid))
        return {"ok": True, "new_eta": new_eta}

    def cancel(self, oid):
        self.db.x("UPDATE orders SET status='cancelled', refunded=1 WHERE id=?", (oid,))
        return {"ok": True, "refund": "full refund issued"}


class HttpOrders:
    """Generic REST adapter. Expected API:
    GET  {base}/orders/{id}          -> order JSON (shape above) or 404
    POST {base}/orders/{id}/refund | /reship | /cancel -> result JSON
    Adjust _call / paths to match Shopify, WooCommerce, your OMS, etc."""

    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def get(self, oid):
        return self._call("GET", f"/orders/{urllib.parse.quote(oid)}")

    def refund(self, oid):
        return self._call("POST", f"/orders/{urllib.parse.quote(oid)}/refund", {})

    def reship(self, oid):
        return self._call("POST", f"/orders/{urllib.parse.quote(oid)}/reship", {})

    def cancel(self, oid):
        return self._call("POST", f"/orders/{urllib.parse.quote(oid)}/cancel", {})


def build_orders(settings, db):
    return HttpOrders(settings.orders_api_url, settings.orders_api_token) if settings.orders_api_url else LocalOrders(db)


class KnowledgeBase:
    """Markdown files in KB_DIR. First line '# Title'. Keyword scoring; swap for a vector store at scale."""

    def __init__(self, directory: str):
        self.docs = []
        if os.path.isdir(directory):
            for name in sorted(os.listdir(directory)):
                if name.endswith(".md"):
                    text = open(os.path.join(directory, name), encoding="utf-8").read()
                    title = re.sub(r"^#\s*", "", text.splitlines()[0]) if text else name
                    self.docs.append({"title": title, "body": text, "tok": tokens(title) | tokens(text)})

    def search(self, query: str, k=2):
        want = tokens(query)
        scored = sorted(((len(want & d["tok"]), d) for d in self.docs), key=lambda t: -t[0])
        return [{"title": d["title"], "body": d["body"][:1500]} for s, d in scored[:k] if s > 0]
