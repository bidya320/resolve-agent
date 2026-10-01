"""Load sample customers/orders for local use:  python seed.py"""
from datetime import date, timedelta

from app.config import get_settings
from app.db import Database


def seed(db):
    d = lambda n: (date.today() + timedelta(days=n)).isoformat()
    for cid, email, name in [("u1", "ana@example.com", "Ana"), ("u2", "ben@example.com", "Ben")]:
        db.x("INSERT OR IGNORE INTO customers VALUES(?,?,?)", (cid, email, name))
    rows = [("1042", "u1", "Wireless earbuds", 59.0, "in_transit", "SwiftShip", d(-4)),
            ("1043", "u1", "Phone case", 15.0, "delivered", "SwiftShip", d(-10)),
            ("1044", "u1", "Desk lamp", 32.0, "processing", None, d(3)),
            ("2001", "u1", "Standing desk", 420.0, "delivered", "FreightCo", d(-8)),
            ("3001", "u2", "Headphones", 120.0, "in_transit", "SwiftShip", d(-6))]
    for r in rows:
        db.x("INSERT OR IGNORE INTO orders(id,customer_id,item,amount,status,carrier,expected_date) VALUES(?,?,?,?,?,?,?)", r)


if __name__ == "__main__":
    seed(Database(get_settings().db_path))
    print("seeded: ana@example.com (orders 1042,1043,1044,2001), ben@example.com (3001)")
