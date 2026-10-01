import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent import Agent, AnthropicLLM
from .config import get_settings
from .db import Database
from .guardrails import RateLimiter
from .integrations import KnowledgeBase, build_orders
from .tools import ToolError, resolve_action

logging.basicConfig(level=logging.INFO)
STATIC = str(Path(__file__).resolve().parent / "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    db = Database(s.db_path)
    orders = build_orders(s, db)
    app.state.s, app.state.db, app.state.orders = s, db, orders
    app.state.limiter = RateLimiter(s.rate_limit_per_min)
    app.state.agent = Agent(db, orders, KnowledgeBase(s.kb_dir), s, AnthropicLLM(s.model))
    yield


app = FastAPI(title="Resolve: autonomous support agent", lifespan=lifespan)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    email: Optional[str] = None
    case_id: Optional[str] = None


class ChatOut(BaseModel):
    case_id: str
    reply: str
    status: str


def identity(request: Request, body_email: Optional[str]) -> str:
    # Production: your auth gateway sets X-Customer-Email and strips it from client requests.
    hdr = request.headers.get("x-customer-email")
    if hdr:
        return hdr.strip().lower()
    if request.app.state.s.allow_body_email and body_email:
        return body_email.strip().lower()
    raise HTTPException(401, "customer identity required")


def admin(request: Request):
    key = request.app.state.s.admin_key
    if not key or not hmac.compare_digest(request.headers.get("x-api-key", ""), key):
        raise HTTPException(403, "forbidden")


@app.get("/")
def home():
    return FileResponse(STATIC + "/index.html")


@app.get("/health")
def health(request: Request):
    request.app.state.db.one("SELECT 1 x")
    return {"ok": True}


@app.post("/api/chat", response_model=ChatOut)
def chat(body: ChatIn, request: Request):
    st, email = request.app.state, identity(request, body.email)
    if not st.limiter.allow(email):
        raise HTTPException(429, "too many messages, slow down")
    case = st.db.case(body.case_id) if body.case_id else None
    if case and case["customer_email"] != email:
        raise HTTPException(404, "case not found")
    case_id = case["id"] if case and case["status"] != "resolved" else st.db.new_case(email)
    reply = st.agent.respond(case_id, email, body.message)
    return ChatOut(case_id=case_id, reply=reply, status=st.db.case(case_id)["status"])


@app.get("/api/cases/{case_id}/messages")
def customer_messages(case_id: str, request: Request, email: Optional[str] = None):
    st, who = request.app.state, identity(request, email)
    case = st.db.case(case_id)
    if not case or case["customer_email"] != who:
        raise HTTPException(404, "case not found")
    out = []
    for m in st.db.messages(case_id):
        c = m["content"]
        text = c if isinstance(c, str) else " ".join(b["text"] for b in c if b.get("type") == "text")
        if text.strip():
            out.append({"role": m["role"], "text": text})
    return {"status": case["status"], "messages": out}


class FeedbackIn(BaseModel):
    case_id: str
    rating: int = Field(ge=1, le=5)
    email: Optional[str] = None


@app.post("/api/feedback")
def feedback(body: FeedbackIn, request: Request):
    st, who = request.app.state, identity(request, body.email)
    case = st.db.case(body.case_id)
    if not case or case["customer_email"] != who or not st.db.set_rating(body.case_id, body.rating):
        raise HTTPException(404, "no resolved case to rate")
    return {"ok": True}


# ---- human support team ---------------------------------------------------
@app.get("/api/admin/metrics", dependencies=[Depends(admin)])
def metrics(request: Request):
    return request.app.state.db.metrics()


@app.get("/api/admin/cases", dependencies=[Depends(admin)])
def list_cases(request: Request, status: Optional[str] = None):
    return request.app.state.db.cases(status)


@app.get("/api/admin/cases/{case_id}", dependencies=[Depends(admin)])
def case_detail(case_id: str, request: Request):
    db = request.app.state.db
    case = db.case(case_id)
    if not case:
        raise HTTPException(404)
    return {"case": case, "messages": db.messages(case_id), "notes": db.notes(case_id),
            "audit": db.audit(case_id), "actions": db.actions(case_id)}


class Decision(BaseModel):
    approve: bool
    note: str = ""


@app.post("/api/admin/actions/{action_id}", dependencies=[Depends(admin)])
def decide(action_id: str, body: Decision, request: Request):
    st = request.app.state
    try:
        return resolve_action(st.db, st.orders, action_id, body.approve, body.note)
    except ToolError as e:
        raise HTTPException(400, str(e))


class Reply(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    resolve: bool = True


@app.post("/api/admin/cases/{case_id}/reply", dependencies=[Depends(admin)])
def human_reply(case_id: str, body: Reply, request: Request):
    db = request.app.state.db
    if not db.case(case_id):
        raise HTTPException(404)
    db.add_message(case_id, "assistant", body.message)
    if body.resolve:
        first = next((m["content"] for m in db.messages(case_id) if m["role"] == "user" and isinstance(m["content"], str)), "")
        db.add_resolution(case_id, first, "Resolved by human", "human")
        db.set_status(case_id, "resolved")
    return {"ok": True}
