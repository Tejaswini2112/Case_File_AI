# CaseFile AI — Complete Production Engineering Roadmap

> A senior AI architect's guide to building a production-grade criminal case research platform from scratch.

---

## The Mentor's First Principle

The biggest mistake engineers make is designing for scale before they have a single user. Twitter didn't build Kubernetes on day one. Airbnb ran on a single server for years. Those engineers weren't less skilled — they were smarter about sequencing.

Production-grade thinking doesn't mean building everything at once. It means building each thing right when it's time.

This roadmap has 5 phases. Each produces a working system. You don't move forward until the current phase is solid. This is how real companies evolve.

---

## What You're Building

```
CaseFile AI — A multi-agent criminal case research platform

Users ask deep questions about criminal cases.
The system searches across primary source documents (FBI files, court records, news).
Multiple specialized agents research, analyze, and synthesize.
Returns cited, structured answers grounded in actual documents.
Supports cross-case analysis ("compare evidence strategies in Bundy vs BTK").
Eventually supports private document upload for journalists/researchers.
```

This is not a chatbot. It's a research intelligence platform. That distinction drives every architectural decision below.

---

# SECTION 1: PROJECT ARCHITECTURE

## 1.1 High-Level Architecture (Final State)

```
┌───────────────────────────────────────────────────────────────┐
│                       CLIENT LAYER                            │
│                  Next.js Web Application                      │
│           (React + TypeScript + TailwindCSS)                  │
└────────────────────────┬──────────────────────────────────────┘
                         │ HTTPS / WebSocket (streaming)
┌────────────────────────▼──────────────────────────────────────┐
│                     API GATEWAY LAYER                         │
│                         FastAPI                               │
│      Auth │ Rate Limiting │ Request Validation │ Routing      │
└─────┬──────────────┬──────────────┬──────────────┬────────────┘
      │              │              │              │
┌─────▼──────┐ ┌────▼──────┐ ┌────▼──────┐ ┌────▼───────────┐
│    RAG     │ │   AGENT   │ │  INGEST   │ │     USER       │
│  SERVICE   │ │  SERVICE  │ │  SERVICE  │ │    SERVICE     │
│            │ │           │ │           │ │                │
│  retrieve  │ │ research  │ │  parse    │ │  auth, prefs   │
│  rerank    │ │ analyze   │ │  chunk    │ │  history       │
│  cite      │ │ write     │ │  embed    │ │  uploads       │
└─────┬──────┘ └────┬──────┘ └────┬──────┘ └────┬───────────┘
      │              │              │              │
┌─────▼──────────────▼──────────────▼──────────────▼────────────┐
│                        DATA LAYER                             │
│                                                               │
│   Pinecone (vectors)   │   PostgreSQL (structured data)       │
│   Redis (cache/queue)  │   S3/R2 (raw document storage)       │
└───────────────────────────────────────────────────────────────┘
      │              │              │
┌─────▼──────────────▼──────────────▼───────────────────────────┐
│                    OBSERVABILITY LAYER                        │
│                                                               │
│   LangFuse (LLM tracing)   │   Prometheus + Grafana (infra)  │
│   Sentry (errors)           │   Custom eval pipeline          │
└───────────────────────────────────────────────────────────────┘
```

## 1.2 User Query Flow (How Components Communicate)

```
User types: "What evidence convicted Ted Bundy in the Chi Omega trial?"
    │
    ▼
API Gateway
    ├── Authenticate user (JWT via Clerk/Supabase)
    ├── Check rate limit (Redis counter)
    ├── Check semantic cache (Redis — have we answered this before?)
    │       └── Cache HIT → return cached response (skip everything below)
    │
    ▼ (Cache MISS)
Orchestrator Agent (the "manager")
    │
    ├──→ Step 1: Query Rewrite Agent
    │         Input:  raw user question
    │         Output: 2-3 precise search queries
    │         Example: ["Chi Omega sorority murders evidence forensic",
    │                   "Bundy trial prosecution Florida bite marks",
    │                   "Ted Bundy conviction Chi Omega witness testimony"]
    │
    ├──→ Step 2: Search Agent (runs searches in parallel)
    │         ├── Pinecone search (ingested FBI files, court docs)
    │         │     → returns top 10 chunks with metadata
    │         ├── Tavily web search (recent articles, analysis)
    │         │     → returns top 5 web results
    │         └── PostgreSQL metadata search (find related cases)
    │               → returns case connections
    │
    ├──→ Step 3: Analysis Agent
    │         Input:  all retrieved chunks + web results
    │         Tasks:  - extract key facts with source attribution
    │                 - identify contradictions between sources
    │                 - flag confidence level for each fact
    │         Output: structured JSON of facts with citations
    │
    └──→ Step 4: Writer Agent
              Input:  analyzed facts + original question
              Tasks:  - compose coherent narrative answer
                      - embed inline citations [Source: FBI Vault, Part 3, p.47]
                      - add confidence indicators
                      - suggest follow-up questions
              Output: streamed markdown response to user
    │
    ▼
Response streamed to frontend via WebSocket/SSE
    │
    ▼
Post-response:
    ├── Cache the response in Redis (semantic cache)
    ├── Log full trace to LangFuse (every agent call, tokens, latency)
    ├── Store query + response in PostgreSQL (user history)
    └── Trigger async eval (did citations match retrieved docs?)
```

## 1.3 Ingestion Pipeline Flow

```
Document Source (FBI Vault PDF, Court Filing, Web Article)
    │
    ▼
Ingestion Service
    │
    ├── Step 1: Parse
    │     ├── PDF → PyMuPDF/pdfplumber (extract text + structure)
    │     ├── HTML → BeautifulSoup (web articles)
    │     └── Store raw file in S3/R2 (original preserved forever)
    │
    ├── Step 2: Pre-process
    │     ├── Clean text (remove headers, footers, page numbers)
    │     ├── Detect document structure (sections, paragraphs)
    │     └── Extract metadata:
    │           {
    │             case_name: "Ted Bundy",
    │             document_type: "FBI Investigation File",
    │             source: "vault.fbi.gov",
    │             date: "1978-03-15",
    │             page_number: 47,
    │             section: "Witness Interview — Nita Neary"
    │           }
    │
    ├── Step 3: Chunk
    │     ├── Strategy: semantic chunking (split at paragraph/section boundaries)
    │     ├── Target size: 400-600 tokens
    │     ├── Overlap: 100 tokens (critical for legal/investigative docs)
    │     └── Preserve parent-child relationships (section → paragraph)
    │
    ├── Step 4: Embed + Store
    │     ├── Upsert to Pinecone with integrated embedding
    │     │     (Pinecone embeds using llama-text-embed-v2 at upsert time)
    │     └── Store chunk metadata in PostgreSQL (for filtering, analytics)
    │
    └── Step 5: Post-process
          ├── Generate document summary (Claude call)
          ├── Extract named entities (people, places, dates)
          ├── Build entity graph in PostgreSQL (for cross-case queries)
          └── Log ingestion metrics to LangFuse
```

## 1.4 Architecture Evolution Over Time

```
Phase 1 (Week 1-3):   Monolith — single Python script, CLI
                       Everything in one file. Learn the mechanics.

Phase 2 (Week 4-6):   Modular Monolith — separate modules, FastAPI, basic UI
                       Clean separation of concerns. Still one deployment unit.

Phase 3 (Week 7-10):  Service-Oriented — Docker Compose, multiple containers
                       RAG, Agent, Ingest as separate services. Shared database.

Phase 4 (Week 11-14): Observable System — LangFuse, evals, caching, async
                       Production-grade monitoring. Semantic caching. Async ingestion.

Phase 5 (Week 15+):   Production — CI/CD, cloud deploy, auth, multi-tenant
                       Real users. Real security. Real scaling.
```

Critical rule: you do NOT design Phase 5 architecture in Week 1. You evolve toward it. Each phase teaches you something that makes the next phase's decisions obvious.

---

# SECTION 2: TECH STACK DECISIONS

Every choice has a "why" — because understanding the reasoning matters more than the choice itself.

## 2.1 Core Application Stack

**Language: Python 3.11+**
Why: 95% of AI tooling is Python-first. FastAPI, LangChain, LlamaIndex, every LLM SDK, every vector DB client — Python first. Not negotiable for AI engineering.

**API Framework: FastAPI**
Why: Async-native (critical for LLM calls that take 2-10 seconds). Automatic OpenAPI docs (your API is self-documenting). Pydantic validation (catch bad data before it hits your agents). WebSocket support for streaming responses. Type hints everywhere. Flask is legacy for new projects. Django is overkill for an API service.

**Frontend: Next.js 14 (App Router) + TypeScript + TailwindCSS**
Why: Server components for fast initial load. React ecosystem (massive library availability). TypeScript prevents bugs at scale — you'll thank yourself in Phase 3. Tailwind is fast to ship and maintains consistency. Server-side rendering matters if you ever want SEO (public case pages).

**LLM Provider: Anthropic Claude (claude-sonnet for agents, claude-haiku for lightweight tasks)**
Why: Best at long-document analysis. Strongest reasoning for complex investigative questions. Excellent tool use / structured output support. Lower hallucination rate than competitors on factual questions. Use Sonnet for analysis/writing agents, Haiku for query rewriting and classification (10x cheaper).

## 2.2 Data Layer

**Vector Database: Pinecone (Serverless)**
Why: Fully managed (you're learning AI engineering, not database ops). Scales to zero when idle (saves real money). Integrated embedding (one less API to manage). Integrated reranking (critical for precision). Excellent metadata filtering (filter by case_name, document_type before vector search). Serverless tier is generous for development.

Alternative considered: Weaviate (more flexible, self-hosted option, but more ops work that distracts from building).

**Relational Database: PostgreSQL (via Supabase or Neon)**
Why: Industry standard. Stores users, sessions, query history, document metadata, entity graphs. Supabase gives you Postgres + auth + realtime subscriptions in one. Neon gives you serverless Postgres that scales to zero. Both have generous free tiers.

What goes in Postgres vs Pinecone:
- Pinecone: vector embeddings + chunk text + metadata for semantic search
- Postgres: user accounts, query logs, document registry, entity relationships, case metadata, analytics

**Cache: Redis (via Upstash)**
Why: Semantic caching (cache similar queries, not just exact matches). Rate limiting (token bucket per user). Session storage. Pub/sub for async job notifications. Upstash is serverless Redis — pay per request, generous free tier. One tool, four use cases.

**Object Storage: Cloudflare R2 (or AWS S3)**
Why: Store raw PDFs and documents permanently. R2 has zero egress fees (significant cost saving when serving documents to users). S3-compatible API so switching is trivial. Never store large files in your database.

**Queue System: Redis Streams (Phase 1-3) → BullMQ or Celery (Phase 4+)**
Why: Document ingestion must be async (parsing a 200-page FBI file shouldn't block your API). Redis Streams is already in your stack via Redis. Simple enough for early phases. Graduate to BullMQ (Node) or Celery (Python) when you need retries, dead letter queues, and priority lanes.

## 2.3 AI/ML Stack

**Embeddings: Pinecone Integrated Embedding (llama-text-embed-v2)**
Why: Pinecone handles embedding at upsert and query time. One less API to call, one less model to manage, one less latency hop. If you later need custom embeddings, you can switch. But start simple.

**Reranking: Pinecone Integrated Reranker (bge-reranker-v2-m3)**
Why: Reranking is the single biggest quality improvement you can make to a RAG system. It re-scores your top-20 retrieved chunks to find the truly best 5. Critical for legal/investigative docs where keyword overlap alone isn't enough. Integrated reranking adds minimal latency.

**Web Search: Tavily API**
Why: Built specifically for AI agents. Returns clean, structured results with content snippets. No HTML parsing needed. Free tier is generous (1000 searches/month). Alternative: Brave Search API.

**Orchestration: Build your own first → LangGraph later**
Why: Start with raw Python and Claude's tool use API. You MUST understand the agent loop (observe → think → act → observe) before using any framework. LangGraph (by LangChain) later for complex multi-agent state machines with conditional routing.

Why NOT LangChain (the main library): it abstracts away things you need to learn. Every production team either uses LangChain minimally or replaces it entirely. Build the core yourself. Use LangGraph only for the state machine part.

**Structured Output: Pydantic + Claude's tool use**
Why: Every agent returns typed, validated output. No regex parsing of LLM text. Define a Pydantic model for each agent's output. Claude's tool use API enforces the schema. This is how production systems work.

## 2.4 Observability & Operations Stack

**LLM Tracing: LangFuse**
Why: THE most important ops tool in your entire stack. Tracks every LLM call with cost, latency, token count, and input/output. Visualizes full agent traces. Built-in prompt management and versioning. Open source. Without this, you're flying blind.

**Error Tracking: Sentry**
Why: Industry standard. Catches unhandled exceptions, gives stack traces with context. Free tier handles 5000 events/month.

**Infrastructure Metrics: Prometheus + Grafana**
Why: Standard for container/infra metrics. CPU, memory, request latency histograms, error rates, queue depth. Add this in Phase 4, not before.

**Evaluation: RAGAS + custom eval scripts**
Why: RAGAS measures faithfulness, answer relevancy, context precision, context recall. Custom scripts for agent-specific evals.

**CI/CD: GitHub Actions**
Why: Free for public repos, generous for private. Excellent Docker support. Simple YAML.

**Containers: Docker + Docker Compose (Phase 1-3) → Kubernetes (Phase 5)**
Why: Docker Compose for local dev and initial deployment. Kubernetes only when you actually need horizontal scaling. K8s before you need it is pure pain with no benefit.

**Cloud: Railway or Fly.io (Phase 1-4) → AWS (Phase 5)**
Why: Railway/Fly.io deploys Docker containers in minutes. AWS for Phase 5 when you need full control. Don't touch AWS until you're ready.

**Authentication: Clerk or Supabase Auth**
Why: Never build auth yourself. Ever. Session management, OAuth, JWT rotation, password hashing — every one has security implications you don't want to handle.

---

# SECTION 3: LEARNING ROADMAP

## 3.1 Skills Progression (What to Learn and When)

```
TIER 1 — Learn BEFORE you start building (Week 0)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Non-negotiable prerequisites:
  ✓ Python fundamentals (functions, classes, async/await, type hints)
  ✓ HTTP basics (GET/POST, status codes, headers, JSON)
  ✓ Git basics (commit, branch, push, pull request)
  ✓ Command line comfort (navigate, run scripts, environment variables)
  ✓ API consumption (calling a REST API with requests/httpx)

TIER 2 — Learn WHILE building Phase 1-2 (Week 1-6)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Learn by doing, not by studying first:
  → FastAPI (routing, Pydantic models, dependency injection)
  → Anthropic API (messages, streaming, tool use, system prompts)
  → Pinecone client (upsert, query, metadata filtering)
  → PDF parsing (PyMuPDF or pdfplumber)
  → Prompt engineering (system prompts, few-shot, chain-of-thought)
  → Basic chunking strategies
  → Environment management (.env, python-dotenv)

TIER 3 — Learn WHILE building Phase 3-4 (Week 7-14)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Build on top of Tier 2:
  → Docker fundamentals (Dockerfile, docker-compose, volumes, networks)
  → PostgreSQL (schemas, queries, migrations with Alembic)
  → Redis (caching patterns, TTL, pub/sub)
  → Async Python (asyncio, httpx.AsyncClient, concurrent agent calls)
  → WebSocket / SSE (streaming responses to frontend)
  → LangFuse integration (tracing, prompt management)
  → RAGAS evaluation framework
  → Next.js basics (for frontend)
  → TypeScript basics

TIER 4 — Learn WHILE building Phase 5 (Week 15+)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Production hardening:
  → CI/CD with GitHub Actions
  → Cloud deployment (Railway/Fly.io first, then AWS)
  → Authentication integration (Clerk/Supabase Auth)
  → Rate limiting implementation
  → Security hardening (input validation, prompt injection defense)
  → Kubernetes fundamentals (only if scaling demands it)
  → Load testing (locust or k6)

TIER 5 — Learn when you want to go deep (ongoing)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Separates good from great:
  → Fine-tuning embedding models on legal domain text
  → GraphRAG (entity relationship graphs for cross-case analysis)
  → Advanced agent patterns (reflection, planning, self-correction)
  → A/B testing for prompts
  → Cost optimization (smart model routing, caching strategies)
  → Multi-tenancy architecture
```

## 3.2 What to Postpone (Common Over-Engineering Mistakes)

```
DO NOT learn/build these until the roadmap says to:

❌ Kubernetes        — until you have traffic that needs horizontal scaling
❌ Terraform         — until you have infra complex enough to manage as code
❌ GraphRAG          — until standard RAG is working perfectly
❌ Fine-tuning       — until you've proven your base system works
❌ Kafka             — until Redis can't handle your message volume
❌ Microservices     — until your monolith has clear service boundaries
❌ Multi-tenancy     — until you have more than one organization using it
❌ Custom embeddings — until Pinecone's integrated embeddings aren't good enough
❌ LangChain         — until you understand the underlying mechanics yourself
```

---

# SECTION 4: SYSTEM DESIGN GUIDANCE

## 4.1 Scalability Considerations

CaseFile AI has three scaling dimensions:

**Read scaling (user queries):**
Bottleneck is LLM API calls (2-15 seconds each). Database queries are fast.
- Semantic caching eliminates repeated similar queries entirely
- Parallel agent execution reduces wall-clock time
- Streaming responses improve perceived performance
- Horizontal scaling: add more API server instances behind a load balancer

**Write scaling (document ingestion):**
Bottleneck is PDF parsing + embedding.
- Async processing via queue (never block the API)
- Batch upserts to Pinecone (send 100 vectors at once, not 1 at a time)
- Parallel chunk processing
- Idempotent ingestion (re-running same document doesn't create duplicates)

**Storage scaling:**
- Pinecone serverless scales automatically
- PostgreSQL: start with Supabase/Neon, upgrade when needed
- S3/R2: effectively infinite

## 4.2 Latency Bottlenecks and Optimization

```
Typical query latency breakdown (unoptimized):

Query rewrite (Claude call)      →  1.5s
Pinecone search                  →  0.2s
Pinecone reranking               →  0.3s
Tavily web search                →  1.0s
Analysis agent (Claude call)     →  3.0s
Writer agent (Claude call)       →  4.0s
────────────────────────────────────────
Total (sequential)               → ~10.0s

After optimization:

Semantic cache check (Redis)     →  0.01s (cache hit = instant response)
Query rewrite (Haiku)            →  0.5s
Pinecone + Tavily (PARALLEL)     →  1.0s (not 1.5s)
Analysis + citation (one call)   →  3.0s
Writer agent (STREAMING)         →  0.3s perceived
────────────────────────────────────────
Total (optimized, cache miss)    → ~4.8s
Perceived by user (streaming)    → ~1.5s to first content
```

Key strategies: parallelize independent calls, use Haiku for lightweight tasks, stream responses, combine agent steps where possible, semantic caching.

## 4.3 Cost Bottlenecks and Optimization

```
Cost for 1,000 queries/day (unoptimized):

Claude Sonnet: 4 calls/query × 1000 × ~$0.01   = ~$40/day
Pinecone: serverless                             = ~$0.10/day
Tavily: free tier then $0.01/search              = ~$0-10/day
Redis/Postgres: free tier                        = $0
────────────────────────────────────────
Total: ~$40/day = ~$1,200/month (almost all LLM costs)

After optimization:

Semantic cache (30% hit rate)    = saves $12/day
Haiku for query rewriting        = $1 instead of $10
2 Sonnet calls instead of 4      = $14/day
────────────────────────────────────────
Optimized: ~$15/day = ~$450/month (63% reduction)
```

Key strategies: model routing (cheapest model per task), semantic caching, context pruning (don't send 50K tokens when 10K will do), batch processing, token tracking via LangFuse.

## 4.4 Caching Strategies

**Layer 1 — Exact query cache (Redis, simple key-value):**
Exact same question → return cached response. TTL: 24 hours.

**Layer 2 — Semantic cache (Redis + embedding similarity):**
Similar questions (>0.92 cosine similarity) → return cached response. Highest-impact layer.

**Layer 3 — Retrieval cache (Redis, cache Pinecone results):**
Cache retrieved chunks for common case queries. TTL: 1 hour.

**Layer 4 — Document summary cache (PostgreSQL):**
Cache per-document summaries at ingestion time. Never expires unless document changes.

## 4.5 Async Processing

**Must be async (never block the API):**
- Document ingestion (parsing + chunking + embedding can take minutes)
- Post-response evaluation
- Analytics aggregation
- Document re-indexing
- Email notifications

**Should be sync (user is waiting):**
- Agent query pipeline (but stream the response)
- Cache lookups
- Authentication checks

```
Implementation progression:
Phase 1-2: Simple background threads (threading module)
Phase 3:   Redis Streams as job queue + worker processes
Phase 4+:  Celery with Redis broker (retries, dead letter queue, priority)
```

## 4.6 Retrieval Optimization

Retrieval quality determines 80% of answer quality, not prompt engineering.

**Optimization cascade:**
1. Query rewriting (turn casual language into precise search terms)
2. Hybrid search (combine vector similarity + keyword matching)
3. Metadata pre-filtering (filter by case_name, document_type BEFORE vector search)
4. Reranking (re-score top-20 results with cross-encoder to get true top-5)
5. Chunk deduplication (don't send overlapping chunks to Claude)
6. Parent-child retrieval (include parent section for context when needed)

## 4.7 Chunking Strategies

```
FIXED-SIZE CHUNKING (400 tokens, 100 overlap)
  Pros: simple, predictable
  Cons: cuts mid-sentence, splits logical sections
  When: Phase 1 — good enough to start

SEMANTIC CHUNKING (split at paragraph/section boundaries)
  Pros: preserves meaning, respects document structure
  Cons: variable chunk sizes, more complex
  When: Phase 2+ — significant quality improvement

HIERARCHICAL CHUNKING (document → section → paragraph → chunk)
  Pros: enables parent retrieval, best for long documents
  Cons: most complex, requires structure detection
  When: Phase 3+ — needed for FBI files with nested sections

AGENTIC CHUNKING (use an LLM to decide chunk boundaries)
  Pros: highest quality boundaries
  Cons: expensive, slow
  When: Phase 4+ — only for high-value documents
```

For CaseFile AI: FBI files have predictable structure (case summaries, witness interviews, evidence logs). Detect these via regex/heuristics, chunk within sections. Each chunk carries metadata: {case, section_type, date, page, participants}.

## 4.8 Memory Architecture

**Short-term memory (within a conversation):**
Store full conversation history in session (Redis). Send relevant history to Claude with each call. Sliding window: keep last 10 exchanges, summarize older ones.

**Long-term memory (across sessions):**
Store past queries and topics in PostgreSQL. Personalize results based on research patterns. Separate per-user namespace for uploaded documents.

**Agent memory (within a multi-agent pipeline):**
Shared scratchpad (JSON object). Each agent reads from and writes to it. Writer agent reads the complete scratchpad to compose the final answer.

## 4.9 Prompt Engineering Architecture

Do NOT hardcode prompts as strings in code.

```
Production prompt management:
Phase 1: Prompts in separate .txt or .yaml files (version controlled)
Phase 2: Prompts in LangFuse (versioned, A/B testable, no redeploy to change)
Phase 3: Prompt templates with variables + few-shot examples
```

Each agent prompt structure:
```
SYSTEM PROMPT:
├── Role definition ("You are a criminal case analysis agent...")
├── Output format (Pydantic schema / tool definition)
├── Constraints ("Only cite information found in provided documents")
├── Few-shot examples (2-3 ideal input/output pairs)
└── Anti-hallucination ("If you don't know, say so explicitly")

USER PROMPT (constructed per-query):
├── Retrieved context (Pinecone chunks, Tavily results)
├── User question
├── Conversation history
└── Metadata hints ("Focus on documents from the Chi Omega case file")
```

## 4.10 Agent Orchestration Design

```
WORKFLOW (deterministic, predefined steps):
  Use when: steps are always the same
  Example: Ingestion pipeline (always: parse → chunk → embed → store)

AGENT (dynamic, decides its own steps):
  Use when: system needs to decide based on input
  Example: Research pipeline (might need 1 search or 5)

MULTI-AGENT (multiple agents coordinated):
  Use when: different subtasks need different expertise
  Example: CaseFile query (search, analyze, write are different skills)
```

CaseFile AI uses Orchestrator-Worker pattern:
```
Orchestrator Agent (manager, Sonnet)
├── Query Rewrite Agent (Haiku — fast, cheap)
├── Search Agent (Haiku — decides which sources to search)
├── Analysis Agent (Sonnet — needs strong reasoning)
└── Writer Agent (Sonnet — needs strong writing)
```

Workers don't talk to each other, only to the orchestrator. Easy to debug, easy to extend.

## 4.11 Failure Handling and Fallback Systems

```
Claude API 429 (rate limited):
  → Exponential backoff with jitter
  → After 5 failures: return cached/degraded response

Claude API 500 (server error):
  → Retry 3 times
  → Last resort: partial cached response + error message

Pinecone returns 0 results:
  → Broaden search (remove metadata filters, increase top_k)
  → Fall back to Tavily web search only
  → Tell user: "Couldn't find relevant documents, here's web info"

Agent produces invalid output:
  → Retry (LLMs are non-deterministic, retry often fixes it)
  → After 2 retries: use raw text, parse what you can
  → Log to LangFuse for investigation

Tavily API down:
  → Skip web search, answer from ingested documents only
  → Inform user: "Web sources temporarily unavailable"

Redis (cache) down:
  → Bypass cache, hit Pinecone directly
  → Slower and costlier, but system still works
```

Cardinal rule: the user should NEVER see a raw error. Always degrade gracefully.

## 4.12 Hallucination Mitigation

This is the highest-stakes concern for CaseFile AI.

**Strategy 1 — Retrieval grounding:**
Every claim MUST trace to a retrieved chunk. Writer agent is instructed to only cite from provided documents.

**Strategy 2 — Structured citations:**
Force structured output: {claim, source, confidence, direct_quote}

**Strategy 3 — Post-hoc verification:**
After writer produces response, run a lightweight Haiku check: "Do these citations match these source chunks?"

**Strategy 4 — Confidence signaling:**
High = directly stated in primary source. Medium = inferred from multiple sources. Low = from web/secondary source. "Not available" = couldn't find evidence.

**Strategy 5 — "I don't know" instruction:**
Every agent prompt: "If the documents don't contain the answer, explicitly state that. Do NOT answer from general knowledge."

## 4.13 Security Concerns

**Prompt injection:** Input sanitization + separate system/user prompt roles + output validation. Never put user input in a system prompt.

**Document security:** When adding private uploads, namespace isolation in Pinecone. Every query includes mandatory user_id filter.

**API key protection:** Environment variables, never in git. Secrets manager in production. Rotate keys periodically.

**Rate limiting:** Per-user (50 queries/hour), global (protect LLM budget). Redis token bucket algorithm.

**Data retention:** Define how long queries are stored. GDPR considerations for EU users.

## 4.14 Multi-Tenancy (Phase 5+)

Each org gets: own Pinecone namespace, own PostgreSQL schema or tenant_id, own API keys, own rate limits, admin dashboard. Don't build until you have 2+ organizations.

---

# SECTION 5: RAG SYSTEM DESIGN (DEEP DIVE)

## 5.1 Ingestion Pipeline — Detailed

```
Step 1: DOCUMENT LOADING
├── FBI PDFs:     PyMuPDF (fast) or pdfplumber (better tables)
│                 or unstructured.io (best overall)
├── Court filings: HTML from CourtListener → BeautifulSoup
├── News articles: Tavily or direct scrape → readability parser
└── Output: raw text + structural metadata per page

Step 2: PRE-PROCESSING
├── Clean headers/footers
├── Fix OCR artifacts
├── Detect section boundaries (regex for "WITNESS INTERVIEW:", etc.)
├── Extract named entities (spaCy NER)
└── Output: cleaned text with section labels and entities

Step 3: CHUNKING
├── First pass: split at section boundaries
├── Second pass: split long sections at paragraph boundaries
├── Enforce: 400-600 token target, 100 token overlap
├── Preserve: complete sentences
├── Enrich metadata:
│     {
│       "chunk_id": "bundy_fbi_part3_p47_c2",
│       "case_name": "Ted Bundy",
│       "document_type": "fbi_file",
│       "source_url": "vault.fbi.gov/ted-bundy/part-3",
│       "page_number": 47,
│       "section_type": "witness_interview",
│       "people_mentioned": ["Nita Neary", "Ted Bundy"],
│       "parent_chunk_id": "bundy_fbi_part3_p47_c1"
│     }

Step 4: EMBED AND STORE
├── Upsert to Pinecone with integrated embedding
├── Store document registry in PostgreSQL
├── Store entity graph in PostgreSQL
└── Upload raw PDF to S3/R2

Step 5: VALIDATION
├── Verify chunk count
├── Sample random chunks for quality
├── Run test query against new document
└── Log metrics
```

## 5.2 Hybrid Retrieval

```
VECTOR SEARCH (semantic):
  "What evidence was used against Bundy?"
  → Finds chunks about forensic evidence even without the word "evidence"

KEYWORD SEARCH (BM25/full-text):
  "Section 8.2 termination clause"
  → Finds exact text matches vector search might miss

HYBRID (combine both):
  Get top-20 from vector + top-20 from keyword
  Merge, deduplicate, rerank → final top-5

Implementation:
  1. Pinecone sparse-dense vectors (built-in hybrid)
  2. Separate Pinecone + PostgreSQL full-text → merge
  3. Reranker to fuse result sets
```

## 5.3 Reranking — Why It Matters

```
Without reranking:
  1. (0.89) General Bundy bio ← high similarity but irrelevant
  2. (0.87) FBI file wrong person ← wrong
  3. (0.85) Bite mark testimony ← ACTUALLY RELEVANT
  4. (0.84) News about documentary ← not primary source
  5. (0.83) Fiber evidence ← ALSO RELEVANT

With reranking (cross-encoder reads query + chunk together):
  1. (0.94) Bite mark testimony ← promoted
  2. (0.91) Fiber evidence ← promoted
  3. (0.72) General bio ← demoted
  4. (0.65) Wrong person ← demoted
  5. (0.61) Documentary news ← demoted
```

## 5.4 Metadata Filtering

Your secret weapon. Filters run BEFORE vector search.

```
User: "What did witnesses say in the Zodiac case?"
│
Apply filters BEFORE search:
  case_name = "Zodiac Killer"
  section_type IN ("witness_interview", "witness_statement")
│
Vector search only runs against matching documents.
Dramatically more relevant results.
```

Critical metadata fields: case_name, document_type, source, date, section_type, people_mentioned, jurisdiction, confidence (primary/secondary/web).

## 5.5 Semantic Caching

```
Traditional:  exact query string → response (misses similar questions)
Semantic:     query embedding → if >0.92 similarity to cached → return cached

Implementation options:
  1. Separate Pinecone index for cache
  2. Redis + custom embedding comparison
  3. GPTCache library

Invalidation:
  - TTL 24 hours
  - Invalidate on new document ingestion for that case
  - Never cache low-confidence responses
```

## 5.6 Citation System

```
LEVEL 1 — Inline: "Evidence was central [FBI Vault, Part 3, p.47]"
LEVEL 2 — Clickable: citation links to chunk in side panel
LEVEL 3 — Source link: "View original document" opens PDF on exact page

All three matter. Legal/investigative context demands verifiability.
```

## 5.7 GraphRAG vs Standard RAG

```
STANDARD RAG:
  question → find similar chunks → answer
  Good for: "What happened in the Chi Omega case?"
  Fails at: "How are Bundy and Dahmer connected?"

GRAPH RAG:
  Build knowledge graph: [Person] --relationship--> [Case/Evidence/Location]
  Now cross-case queries traverse the graph.

Implementation timeline:
  Phase 1-3: Standard RAG (get basics working perfectly)
  Phase 4+:  Add entity extraction → store in PostgreSQL → traverse for cross-case
  Use PostgreSQL with recursive CTEs first. Neo4j only if graphs become primary feature.
```

## 5.8 Multi-Hop Retrieval

```
Simple (single hop):
  "What evidence convicted Bundy?" → search → done

Multi-hop:
  "Did Bundy and Williams trials use the same forensic expert?"
  ├── Hop 1: Find Bundy trial forensic experts
  ├── Hop 2: Find Williams trial forensic experts
  └── Hop 3: Compare results

Detection: 2+ case names, "compare/same/difference" keywords, entity relationships.
Analysis Agent breaks multi-hop queries into sub-queries automatically.
```

## 5.9 Query Rewriting

```
User: "what got bundy caught lol"
↓
Query Rewrite Agent (Haiku, ~$0.001):
  → "Ted Bundy arrest evidence circumstances"
  → "Ted Bundy capture apprehension Florida"
  → "Bundy identification witness testimony"

Multiple queries cover different angles. If one misses, others compensate.
Quality impact: massive — often doubles retrieval relevance.
```

---

# SECTION 6: AGENTIC SYSTEMS (DEEP DIVE)

## 6.1 Agent Loop Mechanics

Before any framework, understand what an agent actually is:

```python
# THE AGENT LOOP (pseudocode)
while not done:
    1. OBSERVE  — current state (query, docs, past actions)
    2. THINK    — decide what to do (LLM reasoning)
    3. ACT      — execute action (call tool, search, write)
    4. OBSERVE  — result of action
    5. DECIDE   — done? need another action?

# This is ALL an agent is. Every framework wraps this loop.

def agent_loop(query, tools, max_steps=10):
    messages = [system_prompt, user_query]
    for step in range(max_steps):
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            messages=messages, tools=tools
        )
        if response.stop_reason == "end_turn":
            return response  # done
        if response.stop_reason == "tool_use":
            tool_result = execute_tool(response.tool_call)
            messages.append(response)
            messages.append(tool_result)
    return "Max steps reached"
```

## 6.2 Reflection and Self-Correction

```
Writer Agent produces response
    ▼
Verifier (Haiku, ~$0.001):
  "Check: does every citation match a source?
   Are there unsupported claims?"
    │
    ├── All supported → send to user
    └── Issues found → Writer re-generates with feedback
         (max 2 retries)
```

## 6.3 State Management

```json
{
  "query_id": "q_abc123",
  "user_query": "What evidence convicted Bundy?",
  "status": "analyzing",
  "rewritten_queries": [...],
  "retrieved_chunks": [...],
  "web_results": [...],
  "analysis": {...},
  "response": null,
  "agent_trace": [
    {"agent": "query_rewrite", "started": "...", "tokens": 150},
    {"agent": "search", "started": "...", "results": 15}
  ],
  "total_tokens": 0,
  "total_cost": 0.0
}
```

This state flows through the pipeline. Every agent reads/writes. Complete state logged to LangFuse. On error, tells you exactly where things broke.

## 6.4 Human-in-the-Loop

```
AMBIGUOUS: "Tell me about the trial"
→ "Which trial? [Bundy Florida 1979] [Bundy Colorado 1977] [OJ Simpson 1995]"

LOW CONFIDENCE:
→ "Limited info found. Expand to web sources?"

SENSITIVE CONTENT:
→ "Documents contain graphic details. Full or summarized version?"
```

Orchestrator includes a "pause_and_ask" tool. Pipeline pauses, resumes with user input.

---

# SECTION 7: EVALUATION & OBSERVABILITY

## 7.1 RAG Evaluation Metrics

```
Faithfulness (most critical):
  "Does the answer only contain info from sources?"
  Score 0-1. Tool: RAGAS. Run on every response.

Answer Relevancy:
  "Does the answer address the question?"
  Score 0-1. Tool: RAGAS. Run on every response.

Context Precision:
  "Were retrieved chunks relevant?"
  Score 0-1. Tool: RAGAS. Run on every response.

Context Recall:
  "Did we find all relevant chunks?"
  Score 0-1. Needs ground truth. Run on eval dataset.

Citation Accuracy (custom):
  "Do citations match source documents?"
  Parse citations, verify against chunks. Run on every response.
```

## 7.2 Evaluation Dataset

Build 50-100 golden Q&A pairs covering: simple fact lookup, evidence analysis, cross-case comparison, timeline construction, entity relationships, follow-ups.

Run the eval suite on every significant code change. This is your regression test.

## 7.3 LangFuse Tracing

```
Every query produces a trace:

TRACE
├── query_rewrite  (Haiku, 150 tokens, $0.001, 0.4s)
├── pinecone_search (2 queries, 15 results, 0.2s)
├── tavily_search   (2 queries, 5 results, 0.9s)
├── analysis_agent  (Sonnet, 4500 tokens, $0.03, 3.1s)
├── writer_agent    (Sonnet, 5200 tokens, $0.04, 4.2s)
└── verification    (Haiku, citation_accuracy: 0.95)

Total: 9900 tokens, $0.071, 8.8s
```

## 7.4 Monitoring Dashboards

```
Dashboard 1 — User Experience:
  Queries/day, avg response time (p50/p95/p99), cache hit rate, error rate

Dashboard 2 — Quality:
  Faithfulness trending, citation accuracy, hallucination rate

Dashboard 3 — Cost:
  LLM spend/day, cost/query, token usage by agent, cache savings

Dashboard 4 — Infrastructure:
  CPU/memory, Redis usage, Pinecone latency, queue depth
```

Tools: Grafana for dashboards. LangFuse for LLM-specific metrics.

---

# SECTION 8: INFRASTRUCTURE & DEPLOYMENT

## 8.1 Docker Strategy

```
Phase 2-3 Setup:

casefile-ai/
├── docker-compose.yml
├── docker-compose.dev.yml      # dev overrides
├── services/
│   ├── api/Dockerfile          # FastAPI
│   ├── worker/Dockerfile       # Ingestion worker
│   └── frontend/Dockerfile     # Next.js
└── infra/
    ├── nginx.conf
    └── prometheus.yml

docker-compose.yml runs:
  api (8000), worker, frontend (3000), redis (6379),
  postgres (5432), langfuse (3001)

One command: docker-compose up
```

Best practices: multi-stage builds, pin dependency versions, non-root user, .dockerignore, health checks.

## 8.2 CI/CD Pipeline

```
On push to main:
  1. Lint (ruff + eslint)
  2. Unit tests (pytest)
  3. Integration tests
  4. Eval suite (quality regression check)
  5. Build Docker images
  6. Push to registry
  7. Deploy to staging
  8. Smoke tests
  9. Deploy to production (manual gate)

Key: eval suite is part of CI. If faithfulness < 0.8, build fails.
```

## 8.3 Cloud Architecture

```
Phase 1-3 (Simple):
  API: Railway, Frontend: Vercel, Redis: Upstash,
  Postgres: Supabase/Neon, Storage: Cloudflare R2
  Cost: $0-20/month

Phase 4-5 (Production):
  API: AWS ECS Fargate, Frontend: Vercel,
  Redis: ElastiCache, Postgres: RDS, Storage: S3,
  Load Balancer: ALB, CDN: CloudFront, DNS: Route53
  Cost: $50-200/month
```

## 8.4 Kubernetes Learning Path

Don't learn until Phase 5, and only if needed. When ready:
1. Understand WHY K8s exists (replicas, auto-restart, rolling updates)
2. Learn building blocks (Pod, Deployment, Service, Ingress)
3. Practice locally (minikube or kind)
4. Use managed K8s (EKS, GKE, or DigitalOcean DOKS)
5. Package with Helm charts

## 8.5 Serverless vs Containers

```
Serverless (Lambda, Vercel Functions):
  ✓ Zero cost at zero traffic, auto-scales
  ✗ Cold starts, time limits, no WebSocket
  → Use for: frontend, cron jobs, webhooks

Containers (ECS, Fly.io, Railway):
  ✓ No cold starts, no time limits, WebSocket support
  ✗ You pay at zero traffic, manage scaling
  → Use for: API server, workers, LangFuse

CaseFile AI: containers for everything except frontend.
Agent pipeline takes 5-15s — serverless cold starts would ruin UX.
```

---

# SECTION 9: DEVELOPMENT PHASES (DETAILED)

## Phase 1 — Working Prototype (Week 1-3)

```
GOAL: Ask a question about an FBI document, get a cited answer.
      Single script. CLI. No UI, no agents, no caching.

Build:
  ├── Download 1 FBI file (Bundy Part 1) from vault.fbi.gov
  ├── Parse PDF → extract text (PyMuPDF)
  ├── Chunk → fixed-size 500 tokens, 100 overlap
  ├── Upsert to Pinecone with basic metadata
  └── Query function: search → build prompt → call Claude → print

Architecture: query.py — one file, everything in functions (~150 lines)

DO NOT build: agents, FastAPI, frontend, Docker, caching, databases

Milestone: type a Bundy question, get a cited answer.
Difficulty: ★★☆☆☆
```

## Phase 2 — Modular System (Week 4-6)

```
GOAL: Multi-agent pipeline, FastAPI, basic Streamlit UI.

Build:
  ├── Refactor into modules (agents/, retrieval/, ingestion/, api/)
  ├── Ingest 3-5 cases
  ├── Multi-agent pipeline (rewrite → parallel search → analyze → write)
  ├── FastAPI with streaming (SSE)
  ├── Basic Streamlit frontend
  ├── Structured output (Pydantic models)
  └── Basic error handling

Milestone: web UI, cross-case queries, streamed cited answers.
Difficulty: ★★★☆☆
```

## Phase 3 — Reliable System (Week 7-10)

```
GOAL: Docker, PostgreSQL, observability, evaluation, proper frontend.

Build:
  ├── Docker Compose
  ├── PostgreSQL (history, document registry, entity graph)
  ├── LangFuse tracing
  ├── Eval pipeline (50 golden Q&A, RAGAS metrics)
  ├── Next.js frontend (search, streaming, citations, case browser)
  ├── Semantic chunking + rich metadata + reranking
  └── Sentry error tracking

Milestone: Dockerized, traced, evaluated, real UI.
Difficulty: ★★★★☆
```

## Phase 4 — Production System (Week 11-14)

```
GOAL: Caching, async, auth, security, optimization.

Build:
  ├── Semantic caching (Redis)
  ├── Async ingestion (Redis Streams + workers)
  ├── Authentication (Clerk/Supabase Auth)
  ├── Security (input sanitization, rate limiting, CORS)
  ├── Model routing (Haiku + Sonnet)
  ├── Follow-ups, case comparison, document upload
  └── CI/CD (GitHub Actions)

Milestone: secure, cached, authenticated, deployed to staging.
Difficulty: ★★★★☆
```

## Phase 5 — Scalable Product (Week 15+)

```
GOAL: Cloud deploy, monitoring, advanced features, polish.

Build:
  ├── Production deployment (Railway/Fly.io or AWS)
  ├── Grafana dashboards + alerting
  ├── GraphRAG for cross-case analysis
  ├── Self-correction loop, research planning
  ├── Saved sessions, export, sharing, mobile-responsive
  └── Load testing, runbooks, documentation

Milestone: public product with monitoring, ready for portfolio.
Difficulty: ★★★★★
```

## What NOT to Over-Engineer

```
Phase 1: No agents. No Docker. No database. Just make one PDF queryable.
Phase 2: No auth. No caching. No deploy. Just make agents work locally.
Phase 3: No security. No cost optimization. Just make it observable.
Phase 4: No Kubernetes. No multi-tenancy. Just make it secure and fast.
Phase 5: Now think about scaling. You'll KNOW what needs it.
```

---

# SECTION 10: RESOURCES

## Documentation (Read First)
```
Anthropic API       → docs.anthropic.com
Pinecone            → docs.pinecone.io
FastAPI             → fastapi.tiangolo.com
LangFuse            → langfuse.com/docs
RAGAS               → docs.ragas.io
Next.js             → nextjs.org/docs
Docker              → docs.docker.com
```

## Courses
```
MANDATORY:
  DeepLearning.AI — "Building Applications with Vector Databases" (free, 1hr)
  DeepLearning.AI — "LangGraph for Agentic AI" (free, ~2hr)
  DeepLearning.AI — "Building Agentic RAG with LlamaIndex" (free, ~2hr)
  DeepLearning.AI — "Evaluating and Debugging Generative AI" (free, ~2hr)

RECOMMENDED:
  freeCodeCamp — "Vector Database Full Course" (YouTube, ~2hr)
  Full Stack Deep Learning — "LLM Bootcamp" (YouTube)
  Arize AI — "LLM Observability Course" (free)

LATER:
  Stanford CS25 — "Transformers United" (YouTube)
  Chip Huyen — "Designing Machine Learning Systems" (book)
```

## YouTube Channels
```
James Briggs      — Pinecone + RAG tutorials
Sam Witteveen     — agents and RAG deep dives
Greg Kamradt      — chunking strategies, RAG evaluation
AI Jason          — production AI system design
Dave Ebbelaar     — Python AI engineering
ArjanCodes        — Python design patterns
Fireship          — fast tech overviews
```

## GitHub Repos to Study
```
MUST STUDY:
  anthropic-cookbook   → official patterns for tool use, RAG, agents
  langgraph           → study examples/ folder for agent patterns
  ragas               → RAG evaluation framework

REFERENCE PROJECTS:
  danswer             → open source RAG chat, excellent architecture
  quivr               → open source RAG + agents, clean codebase
  vercel/ai           → AI SDK for streaming to frontend
```

## Blogs and Newsletters
```
Anthropic blog            → anthropic.com/news
Pinecone learning center  → pinecone.io/learn
LangChain blog            → blog.langchain.dev
Chip Huyen                → huyenchip.com/blog
Eugene Yan                → eugeneyan.com
Latent Space podcast      → latent.space
```

## Papers Worth Reading (read each when you reach that phase)
```
"Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks" — original RAG
"Lost in the Middle" — why chunk position matters
"RAGAS: Automated Evaluation of RAG" — eval framework you'll use
"Self-RAG" — self-correcting RAG
"From Local to Global: A Graph RAG Approach" — Microsoft GraphRAG
"ReAct: Synergizing Reasoning and Acting" — basis for tool-using agents
"Reflexion" — self-reflection in agents
```

---

# SECTION 11: TIME & EFFORT ESTIMATION

## Phase Timeline
```
Phase 1 — Prototype         2-3 weeks    ★★☆☆☆
  Time sink: Pinecone API, prompt iteration
  Feels: "Simpler than I thought"

Phase 2 — Modular           2-3 weeks    ★★★☆☆
  Time sink: agent orchestration, async/streaming
  Feels: "Agents are hard to debug"

Phase 3 — Reliable          3-4 weeks    ★★★★☆
  Time sink: Docker, eval pipeline, frontend
  Feels: "More time on infra than AI"

Phase 4 — Production        3-4 weeks    ★★★★☆
  Time sink: caching edge cases, auth, security
  Feels: "So many things can go wrong"

Phase 5 — Scalable          4+ weeks     ★★★★★
  Time sink: monitoring, polish, advanced features
  Feels: "Now I know why companies have teams for this"

TOTAL: 14-18 weeks (3.5-4.5 months)
Working MVP (Phase 1): 2-3 weeks
Portfolio project (Phase 3): 7-10 weeks
Production-ready (Phase 5): 14-18 weeks
```

## Common Beginner Mistakes
```
1. Starting with framework instead of raw API calls
2. Perfecting prompts too early (will rewrite them all later)
3. Not building eval set (can't tell if changes help or hurt)
4. Over-engineering architecture (Docker in week 1 kills projects)
5. Not tracking costs (accidentally burning $50 in one evening)
6. Ignoring retrieval quality (90% of bad answers = bad retrieval)
7. Not reading error messages (they're specific and helpful)
8. Building alone in silence (share progress, ask communities)
```

## Hardest Concepts
```
HARDEST:
  Agent orchestration, async Python, streaming,
  Docker networking, evaluation design

MEDIUM:
  Prompt engineering, chunking optimization, caching,
  FastAPI, CI/CD

EASIER THAN EXPECTED:
  Pinecone, Claude API, Redis basics,
  PostgreSQL, Railway/Fly.io deployment
```

---

# SECTION 12: REAL-WORLD ENGINEERING MINDSET

## How Production Differs From Tutorials
```
Tutorial:                         Production:
Works on happy path               Handles every unhappy path
10 hardcoded documents             100,000+ documents, growing daily
No error handling                  Graceful degradation everywhere
No monitoring                      Every LLM call traced
"Call OpenAI, print response"      Caching, rate limiting, auth, streaming
Runs once manually                 Runs 24/7 for unknown users
                                   Must not get worse (eval prevents regression)
                                   Must be debuggable at 3 AM
```

## Tradeoffs Engineers Make Daily
```
Accuracy vs Latency:      More agents = better but slower → optimize with metrics
Cost vs Quality:          Sonnet for analysis, Haiku for classification
Freshness vs Stability:   Prioritize ingested docs, supplement with web
Flexibility vs Reliability: Constrain agents with tools + structured output
Build vs Buy:             Build Phase 1-2 yourself, adopt LangGraph Phase 3+
Features vs Polish:       3 great features > 10 mediocre ones
```

## How Startups Evolve
```
Week 1:   "Just make it work" — single script, print debugging
Month 1:  "Not embarrassing" — modules, error handling, .env
Month 3:  "Something broke" — Docker, monitoring, Sentry
Month 6:  "Costs growing" — caching, optimization, model routing
Month 12: "Need reliability" — CI/CD, staging, load testing, auth
Month 18: "Proving scale" — Kubernetes, multi-region, compliance
```

## How to Think Like a Senior AI Engineer
```
1. Measure before optimizing
   "I think caching will help" → WRONG
   "Our p95 is 12s, caching reduces to 3s" → RIGHT

2. Retrieval quality > prompt quality
   Bad answer? Check retrieved chunks first. 90% of the time it's bad context.

3. Simplest solution that could work
   Every line is a liability. Add complexity only with evidence.

4. Make it work, make it right, make it fast
   Phase 1 = work. Phase 2-3 = right. Phase 4-5 = fast.

5. Your eval set is your most important asset
   More valuable than code or prompts. It encodes what "good" looks like.

6. Ship something
   A deployed Phase 1 teaches more than a planned Phase 5.

7. Read production code, not tutorials
   Study Danswer, Quivr, LangGraph source. Tutorials teach syntax.
   Production code teaches judgment.
```

---

# YOUR FIRST 7 DAYS

```
DAY 1:
  □ Create Anthropic API account + key
  □ Create Pinecone account + key
  □ pip install anthropic pinecone pymupdf python-dotenv
  □ Download Ted Bundy FBI file Part 1 from vault.fbi.gov

DAY 2:
  □ Extract text from FBI PDF
  □ Verify extracted text looks correct
  □ Write basic chunking function (500 tokens, 100 overlap)

DAY 3:
  □ Create Pinecone index with integrated embedding
  □ Upsert chunks with metadata
  □ Write search function, verify results

DAY 4:
  □ Build RAG query: search → prompt → Claude → answer
  □ Ask 10 questions. Note what works and what doesn't.

DAY 5:
  □ Improve prompts based on Day 4
  □ Add citation instructions
  □ Start golden eval set (10 Q&A pairs)

DAY 6:
  □ Ingest second case (Zodiac or Manson)
  □ Add metadata filtering by case_name
  □ Test cross-case queries

DAY 7:
  □ Clean up code into functions
  □ Simple CLI interface
  □ Push to GitHub with README
  □ Celebrate — Phase 1 is working
```

---

*This document is your roadmap. Refer back to it at every phase transition. The technologies may evolve, but the architecture principles and engineering mindset are timeless.*

*Now go download that FBI file and start building.*
