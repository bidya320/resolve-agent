# Resolve: Autonomous Customer Support Resolution Agent

Production-oriented agent that understands a customer message, plans, calls real business tools
(orders, refunds, knowledge base, CRM), resolves the case, hands off to humans when needed, and
learns from customer feedback.

```
Customer -> FastAPI /api/chat -> Agent (Claude tool loop) -> Tools (policy enforced server-side)
                                        |                        |-- OrderSystem (local DB or your REST API)
                                        |                        |-- KnowledgeBase (markdown, swap for vector DB)
                                        |                        '-- CRM notes, audit log, approvals
Human team <- /api/admin/* (queue, approve refunds, reply, metrics)
```

## What makes it real-world
| Concern | How it's handled |
|---|---|
| Identity | Tools get the customer email from the platform, never from the model. Orders of other customers return "not found". |
| Policy | Refund/re-ship/cancel rules live in `app/tools.py`, not the prompt. Refunds above `MAX_AUTO_REFUND` create a pending approval. |
| Human handoff | `escalate_to_human`, step limit, LLM outage and tool failures all escalate. Held cases never call the LLM. |
| Audit | Every tool call (args, result, ok) is stored; full transcripts and CRM notes per case. |
| Privacy | Card-like numbers are redacted before storage and before reaching the model. |
| Abuse | Per-customer rate limit, input length caps, id validation, admin endpoints need `X-API-Key`. |
| Learning | Customers rate resolved cases (1-5). Only agent-resolved cases rated 4+ are fed back as hints for similar queries. Retrieval memory, not model training. |
| KPIs | `GET /api/admin/metrics`: automation rate, status counts, average rating. |

## Run locally
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...  ADMIN_API_KEY=devkey
python seed.py                      # sample data: ana@example.com orders 1042/1043/1044/2001
uvicorn app.main:app --reload       # chat UI at http://localhost:8000
pytest                              # 14 tests, no API key needed (fake LLM)
```
Try as ana@example.com: "My order 1042 hasn't arrived", "refund order 1043", "refund 2001" (goes to approval), "I forgot my password".

Human side:
```bash
curl -H "X-API-Key: devkey" localhost:8000/api/admin/cases?status=pending_approval
curl -H "X-API-Key: devkey" -X POST localhost:8000/api/admin/actions/A-xxxx -H 'content-type: application/json' -d '{"approve":true}'
```

## Connect your real systems
1. **Orders / payments**: set `ORDERS_API_URL` + `ORDERS_API_TOKEN`; adapt `HttpOrders` in `app/integrations.py` (Shopify, WooCommerce, your OMS). Contract is in the file docstring.
2. **Knowledge base**: drop `.md` files in `data/kb/` (first line `# Title`).
3. **Identity**: put the service behind your login. Your gateway sets `X-Customer-Email` (and strips it from client requests); set `ALLOW_BODY_EMAIL=0`.
4. **Policy**: edit the rules in `app/tools.py` (refund window, limits, who can be re-shipped).

## Deploy
`docker compose up --build` (needs `.env` from `.env.example`). Mount `/data` on a persistent volume.

## Before go-live checklist
- Move SQLite to Postgres (all SQL is in `app/db.py`) if you run more than one instance.
- Terminate TLS, set a strong `ADMIN_API_KEY`, restrict `/api/admin/*` to your network or SSO.
- Replace in-memory rate limiting with Redis when scaling horizontally.
- Shadow-test on real tickets first (agent proposes, humans act), then raise autonomy gradually.
- Review data-retention and privacy rules for stored transcripts in your region.
- Add monitoring (errors, latency, escalation rate, refund totals) and alert on anomalies.

## Tested vs not tested
Verified here: database, tools, policy, guardrails, agent loop with a scripted fake LLM (14 tests pass).
Not executed in this environment (no packages/network): the FastAPI layer, the live Anthropic API call, and Docker build.
Run `pytest` plus a real conversation in staging before relying on it.
