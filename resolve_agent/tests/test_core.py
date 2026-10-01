from app.agent import Agent, FALLBACK, HELD
from app.config import Settings
from app.db import Database
from app.guardrails import RateLimiter, redact
from app.integrations import KnowledgeBase, LocalOrders
from app.tools import Ctx, execute, resolve_action
from seed import seed
import json


class FakeLLM:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def complete(self, system, messages, tools):
        self.calls += 1
        if isinstance(self.script[0], Exception):
            raise self.script.pop(0)
        return self.script.pop(0)


def tu(i, name, **args):
    return [{"type": "tool_use", "id": i, "name": name, "input": args}]


def text(t):
    return [{"type": "text", "text": t}]


def make(llm=None, **kw):
    s = Settings(db_path=":memory:", kb_dir="data/kb", **kw)
    db = Database(":memory:")
    seed(db)
    orders = LocalOrders(db)
    kb = KnowledgeBase("data/kb")
    ctx = lambda email="ana@example.com": Ctx(db.new_case(email), email, db, orders, kb, s)
    return s, db, orders, kb, ctx, (Agent(db, orders, kb, s, llm) if llm else None)


def test_cannot_read_other_customers_order():
    *_, ctx, _a = make()
    out = json.loads(execute(ctx("ben@example.com"), "get_order_status", {"order_id": "1042"}))
    assert out == {"error": "Order not found"}


def test_refund_within_limit_then_blocked_twice():
    _s, db, orders, _k, ctx, _a = make()
    c = ctx()
    assert json.loads(execute(c, "issue_refund", {"order_id": "1043"}))["ok"]
    assert "already refunded" in execute(c, "issue_refund", {"order_id": "1043"})


def test_large_refund_needs_human_approval():
    _s, db, orders, _k, ctx, _a = make()
    c = ctx()
    out = json.loads(execute(c, "issue_refund", {"order_id": "2001"}))
    assert out["status"] == "pending_human_approval"
    assert db.case(c.case_id)["status"] == "pending_approval"
    assert orders.get("2001")["refunded"] is False          # nothing moved yet
    resolve_action(db, orders, out["action_id"], True)
    assert orders.get("2001")["refunded"] is True and db.case(c.case_id)["status"] == "resolved"


def test_reship_policy():
    _s, db, orders, _k, ctx, _a = make()
    c = ctx()
    assert json.loads(execute(c, "reship_order", {"order_id": "1042"}))["ok"]
    assert "already" in execute(c, "reship_order", {"order_id": "1042"})
    assert "error" in execute(c, "reship_order", {"order_id": "1043"})   # delivered, not late


def test_cancel_only_processing():
    *_, ctx, _a = make()
    c = ctx()
    assert "error" in execute(c, "cancel_order", {"order_id": "1042"})
    assert json.loads(execute(c, "cancel_order", {"order_id": "1044"}))["ok"]


def test_unknown_tool_and_bad_args():
    *_, ctx, _a = make()
    c = ctx()
    assert "unknown tool" in execute(c, "drop_database", {})
    assert "error" in execute(c, "get_order_status", {"wrong": 1})
    assert "Invalid order id" in execute(c, "get_order_status", {"order_id": "1; DROP TABLE orders"})


def test_agent_full_resolution_and_audit():
    llm = FakeLLM([tu("1", "get_order_status", order_id="1042"), tu("2", "reship_order", order_id="1042"),
                   tu("3", "update_crm", note="late, reshipped"), tu("4", "close_case", resolution="reshipped late order"),
                   text("Sorry for the delay - a replacement is on its way.")])
    _s, db, orders, _k, _c, agent = make(llm)
    cid = db.new_case("ana@example.com")
    reply = agent.respond(cid, "ana@example.com", "My order 1042 has not arrived")
    assert "replacement" in reply
    assert db.case(cid)["status"] == "resolved"
    assert [a["tool"] for a in db.audit(cid)] == ["get_order_status", "reship_order", "update_crm", "close_case"]
    assert orders.get("1042")["reshipped"] is True


def test_llm_failure_escalates():
    _s, db, *_r, agent = make(FakeLLM([RuntimeError("api down")]))
    cid = db.new_case("ana@example.com")
    assert agent.respond(cid, "ana@example.com", "hi") == FALLBACK
    assert db.case(cid)["status"] == "escalated"


def test_step_limit_escalates():
    llm = FakeLLM([tu(str(i), "update_crm", note="loop") for i in range(10)])
    _s, db, *_r, agent = make(llm, max_steps=3)
    cid = db.new_case("ana@example.com")
    agent.respond(cid, "ana@example.com", "x")
    assert db.case(cid)["status"] == "escalated" and llm.calls == 3


def test_held_case_does_not_call_llm():
    llm = FakeLLM([text("should not happen")])
    _s, db, *_r, agent = make(llm)
    cid = db.new_case("ana@example.com")
    db.set_status(cid, "escalated")
    assert agent.respond(cid, "ana@example.com", "any update?") == HELD and llm.calls == 0


def test_agent_cannot_close_held_case():
    _s, db, orders, _k, ctx, _a = make()
    c = ctx()
    db.set_status(c.case_id, "escalated")
    assert "held by a human" in execute(c, "close_case", {"resolution": "done"})


def test_card_numbers_redacted_before_storage():
    llm = FakeLLM([text("ok")])
    _s, db, *_r, agent = make(llm)
    cid = db.new_case("ana@example.com")
    agent.respond(cid, "ana@example.com", "my card 4111 1111 1111 1111 was charged twice")
    assert "4111" not in json.dumps(db.messages(cid)) and "[card number removed]" in redact("4111-1111-1111-1111")


def test_learning_uses_only_well_rated_agent_cases():
    _s, db, *_ = make()
    db.add_resolution("c1", "order late not arrived", "re-shipped", "agent")
    db.add_resolution("c2", "order late not arrived", "bad answer", "agent")
    assert db.recall("my order is late") == []               # nothing rated yet
    db.set_rating("c1", 5)
    db.set_rating("c2", 1)
    assert [r["summary"] for r in db.recall("my order is late")] == ["re-shipped"]


def test_kb_search_and_rate_limit_and_metrics():
    _s, db, _o, kb, *_ = make()
    assert kb.search("forgot password")[0]["title"] == "Reset your password"
    rl = RateLimiter(2)
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a") and rl.allow("b")
    assert db.metrics()["cases"] == 0
