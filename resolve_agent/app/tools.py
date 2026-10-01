"""Tools the agent may call. ALL policy is enforced here, server-side, so a prompt injection
or a model mistake cannot exceed what the business allows."""
import json, logging, re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("agent.tools")
ORDER_ID = re.compile(r"^[A-Za-z0-9-]{3,20}$")


class ToolError(Exception):
    pass


@dataclass
class Ctx:
    case_id: str
    customer_email: str  # set by the platform from authenticated identity, never by the model
    db: Any
    orders: Any
    kb: Any
    settings: Any


def _order(ctx, order_id):
    oid = str(order_id).strip().lstrip("#")
    if not ORDER_ID.match(oid):
        raise ToolError("Invalid order id")
    o = ctx.orders.get(oid)
    if not o or o["customer_email"].lower() != ctx.customer_email.lower():
        raise ToolError("Order not found")  # same message for 'not yours' -> no order enumeration
    return o


def search_knowledge_base(ctx, query):
    return {"articles": ctx.kb.search(str(query))}


def get_order_status(ctx, order_id):
    o = _order(ctx, order_id)
    return {k: o[k] for k in ("id", "item", "status", "days_late", "amount", "refunded", "reshipped")}


def reship_order(ctx, order_id):
    o = _order(ctx, order_id)
    if o["reshipped"]:
        raise ToolError("Order was already re-shipped once")
    if o["status"] != "in_transit" or o["days_late"] < ctx.settings.late_days_for_remedy:
        raise ToolError(f"Re-ship only allowed for in-transit orders at least {ctx.settings.late_days_for_remedy} days late")
    return ctx.orders.reship(o["id"])


def issue_refund(ctx, order_id):
    o = _order(ctx, order_id)
    if o["refunded"] or o["status"] == "refunded":
        raise ToolError("Order already refunded")
    eligible = o["status"] == "delivered" or (o["status"] == "in_transit" and o["days_late"] >= ctx.settings.late_days_for_remedy)
    if not eligible:
        raise ToolError(f"Status '{o['status']}' is not refundable (processing orders: use cancel_order)")
    if o["amount"] > ctx.settings.max_auto_refund:
        aid = ctx.db.add_action(ctx.case_id, "refund", {"order_id": o["id"], "amount": o["amount"]})
        ctx.db.set_status(ctx.case_id, "pending_approval")
        return {"status": "pending_human_approval", "action_id": aid,
                "message": "Refund is above the automatic limit; a teammate will approve it."}
    return ctx.orders.refund(o["id"])


def cancel_order(ctx, order_id):
    o = _order(ctx, order_id)
    if o["status"] != "processing":
        raise ToolError(f"Cannot cancel an order that is '{o['status']}'")
    return ctx.orders.cancel(o["id"])


def update_crm(ctx, note):
    ctx.db.add_note(ctx.case_id, str(note))
    return {"ok": True}


def escalate_to_human(ctx, reason):
    ctx.db.add_note(ctx.case_id, f"ESCALATED: {reason}")
    ctx.db.set_status(ctx.case_id, "escalated")
    return {"ok": True, "message": "A teammate will follow up."}


def close_case(ctx, resolution):
    case = ctx.db.case(ctx.case_id)
    if case["status"] in ("escalated", "pending_approval"):
        raise ToolError("Case is held by a human and cannot be closed by the agent")
    first = next((m["content"] for m in ctx.db.messages(ctx.case_id) if m["role"] == "user" and isinstance(m["content"], str)), "")
    ctx.db.add_note(ctx.case_id, f"RESOLVED: {resolution}")
    ctx.db.add_resolution(ctx.case_id, first, str(resolution), "agent")
    ctx.db.set_status(ctx.case_id, "resolved")
    return {"ok": True}


REGISTRY = {f.__name__: f for f in (search_knowledge_base, get_order_status, reship_order, issue_refund,
                                    cancel_order, update_crm, escalate_to_human, close_case)}


def _spec(name, desc, *params):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": {p: {"type": "string"} for p in params},
                             "required": list(params)}}


TOOL_SPECS = [
    _spec("search_knowledge_base", "Search FAQ / policy docs.", "query"),
    _spec("get_order_status", "Look up one of the customer's orders.", "order_id"),
    _spec("reship_order", "Send a free replacement for a badly delayed in-transit order.", "order_id"),
    _spec("issue_refund", "Refund a delivered or badly delayed order (large amounts go to human approval).", "order_id"),
    _spec("cancel_order", "Cancel an order that has not shipped yet (full refund).", "order_id"),
    _spec("update_crm", "Write a short note on the case record.", "note"),
    _spec("escalate_to_human", "Hand the case to a human: unsure, out of policy, angry customer, repeated tool failure.", "reason"),
    _spec("close_case", "Mark the case resolved after the customer's issue is fully handled.", "resolution"),
]


def execute(ctx: Ctx, name: str, args: dict) -> str:
    fn = REGISTRY.get(name)
    if fn is None:
        out, ok = {"error": "unknown tool"}, False
    else:
        try:
            out, ok = fn(ctx, **(args or {})), True
        except ToolError as e:
            out, ok = {"error": str(e)}, False
        except TypeError:
            out, ok = {"error": "invalid arguments"}, False
        except Exception:
            log.exception("tool %s failed", name)
            out, ok = {"error": "tool temporarily unavailable"}, False
    result = json.dumps(out)
    ctx.db.add_audit(ctx.case_id, name, args, result, ok)
    return result


def resolve_action(db, orders, action_id: str, approve: bool, reviewer_note: str = "") -> dict:
    """Human decision on a pending refund."""
    a = db.action(action_id)
    if not a or a["status"] != "pending":
        raise ToolError("No pending action with that id")
    if approve:
        res = orders.refund(a["args"]["order_id"])
        db.set_action(action_id, "approved")
        db.add_note(a["case_id"], f"Refund approved by human. {reviewer_note}")
        db.add_resolution(a["case_id"], "refund over limit", "Refund approved by human", "human")
        db.set_status(a["case_id"], "resolved")
        return res
    db.set_action(action_id, "rejected")
    db.add_note(a["case_id"], f"Refund rejected by human. {reviewer_note}")
    db.set_status(a["case_id"], "escalated")
    return {"ok": True, "rejected": True}
