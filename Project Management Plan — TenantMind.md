# PROJECT MANAGEMENT PLAN

## TenantMind — Multi-Tenant Enterprise AI Knowledge Platform

**Project Type:** Final Year Major Project\
**Domain:** Artificial Intelligence / Generative AI / Backend Engineering / SaaS\
**Primary Technologies:** Python, FastAPI, PostgreSQL, Qdrant, Redis, Celery, Next.js, Docker\
**Architecture:** Multi-Tenant SaaS + RAG\
**Development Approach:** Agile / Incremental Development

---

# 1. Executive Summary

TenantMind is a secure, multi-tenant enterprise knowledge platform that enables organizations to upload internal documents and interact with them through a Retrieval-Augmented Generation (RAG) based AI assistant.

Unlike a conventional document chatbot, TenantMind is designed as a production-oriented SaaS platform where multiple organizations can use the same application while maintaining strict isolation of their users, documents, vector data, and queries.

The platform implements:

- Multi-tenant architecture
- JWT-based authentication
- Role-Based Access Control (RBAC)
- Tenant-isolated document storage
- Asynchronous document ingestion
- Vector-based semantic retrieval
- Hybrid dense + keyword retrieval
- Reranking
- Document-level access control
- Citation-grounded LLM responses
- Server-Sent Events (SSE) streaming
- Redis caching and rate limiting
- Audit and query logging
- RAG evaluation
- Automated security and tenant-isolation testing
- Docker-based deployment

The primary objective is to demonstrate how modern RAG systems can be integrated into a secure, scalable backend architecture rather than building a simple question-answering chatbot.

---

# 2. Problem Statement

Organizations maintain large amounts of internal information in documents such as:

- Employee handbooks
- HR policies
- Technical documentation
- Standard Operating Procedures
- Product documentation
- Internal guides
- Compliance documents
- Company FAQs

Traditional approaches require employees to manually search through these documents.

Generic AI chatbots introduce another problem: sensitive organizational information may be exposed across users or organizations if proper access controls are not implemented.

Therefore, there is a need for a system that combines:

1. Secure multi-tenant architecture
2. Role-based document access
3. Efficient document retrieval
4. Grounded LLM responses
5. Source citations
6. Scalable asynchronous ingestion
7. Measurable retrieval and answer quality

TenantMind addresses these requirements through a secure RAG-based SaaS architecture.

---

# 3. Project Objectives

## 3.1 Primary Objectives

The project aims to:

1. Develop a multi-tenant enterprise knowledge platform.
2. Implement secure authentication and authorization.
3. Ensure strict isolation between organizations.
4. Build an asynchronous document ingestion pipeline.
5. Implement semantic and keyword-based retrieval.
6. Implement reranking for improved retrieval quality.
7. Generate grounded responses using an LLM.
8. Provide citations for retrieved information.
9. Implement document-level access control.
10. Evaluate retrieval and generation quality using measurable metrics.

## 3.2 Secondary Objectives

The project will also:

- Implement API rate limiting.
- Implement response caching.
- Provide real-time streaming responses.
- Maintain query and audit logs.
- Provide administrative usage information.
- Containerize the complete system.
- Implement automated tests and CI/CD.
- Deploy a working production-like instance.

---

# 4. Project Scope

## 4.1 In Scope

### Authentication

- Tenant registration
- User registration/login
- JWT access tokens
- Refresh tokens
- Password hashing
- Token revocation

### Authorization

- Admin role
- Editor role
- Viewer role
- Route-level authorization
- Document-level access control

### Document Management

- Upload documents
- List documents
- View document status
- Delete documents
- Track ingestion errors
- Track chunk counts

### RAG

- Document parsing
- Text extraction
- Chunking
- Embedding generation
- Vector storage
- Dense retrieval
- BM25/keyword retrieval
- Hybrid retrieval
- Reranking
- Context construction
- LLM generation
- Source citations

### Platform

- Multi-tenancy
- PostgreSQL
- Redis
- Background workers
- Qdrant
- API logging
- Rate limiting
- SSE streaming

### Evaluation

- Retrieval evaluation
- RAG evaluation
- Tenant isolation tests
- RBAC tests
- API integration tests

---

# 5. Out of Scope

The following features are intentionally excluded from the initial version:

- Training a custom LLM
- Building a custom embedding model
- Fine-tuning an LLM
- Native mobile applications
- Voice-based interaction
- Automatic legal or medical advice
- Enterprise SSO/SAML
- Complex billing/payment integration
- Large-scale distributed Kubernetes deployment
- Fully autonomous AI agents

These may be considered future enhancements.

---

# 6. Target Users

## 6.1 Organization Administrator

Responsible for:

- Managing users
- Assigning roles
- Uploading documents
- Deleting documents
- Monitoring usage
- Viewing document processing status

## 6.2 Editor

Responsible for:

- Uploading documents
- Managing permitted documents
- Querying the knowledge base

## 6.3 Viewer

Responsible for:

- Querying authorized organizational information
- Viewing generated answers and citations

---

# 7. Functional Requirements

## FR-01: Tenant Registration

The system shall allow an organization to create a tenant account.

## FR-02: Authentication

The system shall authenticate users using secure credentials and JWT tokens.

## FR-03: Role Management

Administrators shall be able to assign roles to users.

## FR-04: Document Upload

Authorized users shall be able to upload supported documents.

## FR-05: Asynchronous Processing

Uploaded documents shall be processed asynchronously.

## FR-06: Document Indexing

The system shall parse, chunk, embed, and index document content.

## FR-07: Tenant Isolation

Users shall only be able to access information belonging to their organization.

## FR-08: Document-Level Authorization

Documents shall support role-based access restrictions.

## FR-09: Semantic Search

The system shall retrieve semantically relevant document chunks.

## FR-10: Hybrid Retrieval

The system shall combine dense retrieval with keyword/BM25 retrieval.

## FR-11: Reranking

Retrieved results shall be reranked before being passed to the LLM.

## FR-12: Grounded Generation

The LLM shall generate answers using retrieved context.

## FR-13: Citations

Generated responses shall provide references to source documents/chunks.

## FR-14: Streaming

The query endpoint shall support streaming responses using SSE.

## FR-15: Logging

Queries and relevant usage information shall be logged.

---

# 8. Non-Functional Requirements

## Security

- Passwords must never be stored in plaintext.
- JWT access tokens must have short expiration times.
- Refresh tokens must be revocable.
- Tenant identity must come from authenticated identity rather than arbitrary request data.
- Cross-tenant access must be prevented.
- Uploaded files must be validated.

## Performance

Target initial performance:

- API response for document submission: < 1 second excluding network overhead
- Retrieval: < 500 ms under normal development load
- Streaming response should begin quickly after retrieval
- Document processing should occur asynchronously

These are engineering targets, not guaranteed production SLAs.

## Scalability

The architecture should allow independent scaling of:

- API servers
- Worker processes
- PostgreSQL
- Redis
- Vector storage

## Reliability

- Failed ingestion jobs must be recorded.
- Workers should support retries.
- API errors should return structured responses.
- The system should expose a health endpoint.

## Maintainability

The application should use:

- Modular architecture
- Service/repository separation
- Pydantic schemas
- Type hints
- Environment-based configuration
- Automated tests

---

# 9. System Architecture

```text
                         ┌──────────────────────┐
                         │    Next.js Frontend  │
                         └──────────┬───────────┘
                                    │
                              HTTPS / SSE
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │      FastAPI API     │
                         │                      │
                         │ JWT │ RBAC │ Rate    │
                         │ Limit │ Request ID   │
                         └──────────┬───────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
              ▼                     ▼                     ▼
        PostgreSQL                Redis                Qdrant
              │                     │                     │
              │                     ▼                     │
              │               Task Queue                  │
              │                     │                     │
              │                     ▼                     │
              │              ┌──────────────┐             │
              │              │ Celery Worker│             │
              │              └──────┬───────┘             │
              │                     │                     │
              │              Parse → Chunk → Embed       │
              │                     │                     │
              │                     └────────────────────►│
              │                                           │
              └───────────────────────────────────────────┘
```

---

# 10. Upload / Write Path

```text
User
 ↓
POST /documents
 ↓
JWT Authentication
 ↓
RBAC Verification
 ↓
File Validation
 ↓
Object Storage
 ↓
PostgreSQL Document Record
 ↓
Queue Ingestion Job
 ↓
202 Accepted
```

Background processing:

```text
Worker
 ↓
Read Document
 ↓
Parse
 ↓
Clean Text
 ↓
Chunk
 ↓
Generate Embeddings
 ↓
Upsert into Qdrant
 ↓
Update Document Status
```

Possible states:

```text
processing
ready
failed
```

---

# 11. Query / Read Path

```text
User Question
      ↓
JWT Authentication
      ↓
Tenant Identification
      ↓
Rate Limit Check
      ↓
Redis Cache
      ↓
Query Embedding
      ↓
Hybrid Retrieval
      ├── Dense Search
      └── BM25
      ↓
Tenant + Role Filters
      ↓
Top 20 Results
      ↓
Reranker
      ↓
Top 5 Context Chunks
      ↓
Prompt Construction
      ↓
LLM
      ↓
SSE Streaming
      ↓
Answer + Citations
      ↓
Query Logging
```

---

# 12. Multi-Tenant Data Isolation

The system will use tenant-aware data access throughout the application.

Conceptually:

```text
Tenant A
 ├── Users
 ├── Documents
 ├── Query Logs
 └── Vector Data

Tenant B
 ├── Users
 ├── Documents
 ├── Query Logs
 └── Vector Data
```

Every database repository will require a tenant identifier.

Example:

```python
get_documents(
    tenant_id=current_user.tenant_id
)
```

The application must never trust a tenant ID supplied by the client.

The authenticated identity determines the tenant context.

---

# 13. Vector Database Strategy

### Initial implementation

Use:

```text
Qdrant
```

with one collection per tenant:

```text
tenant_001
tenant_002
tenant_003
```

Each vector point will contain metadata:

```json
{
  "document_id": "doc_123",
  "chunk_index": 12,
  "page": 4,
  "text": "...",
  "allowed_roles": ["admin", "editor"],
  "filename": "hr_policy.pdf"
}
```

### Scalability consideration

For a very large number of tenants, the architecture can be changed to a shared collection with mandatory `tenant_id` filtering.

The project documentation will discuss the trade-off between:

- Per-tenant collections
- Shared collection with tenant filtering

---

# 14. Database Design

## Tenants

```text
tenants
---------
id
name
slug
plan
created_at
```

## Users

```text
users
---------
id
tenant_id
email
password_hash
role
is_active
created_at
```

## Documents

```text
documents
---------
id
tenant_id
uploaded_by
filename
storage_key
status
allowed_roles
chunk_count
error
created_at
```

## Query Logs

```text
query_logs
---------
id
tenant_id
user_id
question
answer
source_chunk_ids
latency_ms
feedback
created_at
```

## Refresh Tokens

```text
refresh_tokens
---------
id
user_id
token_hash
expires_at
revoked
created_at
```

---

# 15. API Design

## Authentication

```http
POST /auth/register-tenant
POST /auth/login
POST /auth/refresh
POST /auth/logout
```

## Users

```http
POST /users/invite
GET /users
PATCH /users/{id}/role
DELETE /users/{id}
```

## Documents

```http
POST /documents
GET /documents
GET /documents/{id}
DELETE /documents/{id}
```

## Query

```http
POST /query
GET /query/history
POST /query/{id}/feedback
```

## Administration

```http
GET /admin/usage
GET /admin/documents/{id}/status
```

## Operations

```http
GET /health
GET /health/ready
```

---

# 16. Role-Based Access Control

| Operation            | Admin | Editor | Viewer |
| -------------------- | ----: | -----: | -----: |
| Query knowledge base |     ✓ |      ✓ |      ✓ |
| View documents       |     ✓ |      ✓ |      ✓ |
| Upload documents     |     ✓ |      ✓ |      ✗ |
| Delete documents     |     ✓ |      ✗ |      ✗ |
| Invite users         |     ✓ |      ✗ |      ✗ |
| Change user roles    |     ✓ |      ✗ |      ✗ |
| View usage analytics |     ✓ |      ✗ |      ✗ |

Document-level permissions provide an additional authorization layer.

---

# 17. Security Architecture

The project will implement:

### Authentication

- Argon2 or bcrypt password hashing
- Short-lived JWT access tokens
- Refresh tokens
- Token revocation

### Authorization

- Route-level RBAC
- Document-level role filtering
- Tenant-aware repository functions

### API Security

- Request IDs
- Rate limiting
- Input validation
- File size limits
- MIME/content validation
- CORS configuration
- Secure environment variables

### RAG Security

Retrieved document content will be treated as untrusted data.

The LLM prompt will explicitly instruct the model not to follow instructions contained inside retrieved documents.

The project will also test basic prompt-injection scenarios.

---

# 18. RAG Pipeline

## Ingestion

```text
Document
 ↓
Parser
 ↓
Text Extraction
 ↓
Cleaning
 ↓
Semantic/Recursive Chunking
 ↓
Metadata Generation
 ↓
Embedding Model
 ↓
Qdrant
```

## Retrieval

```text
Question
 ↓
Query Processing
 ↓
Dense Search ───┐
                ├── Hybrid Fusion
BM25 Search ────┘
 ↓
Top K
 ↓
Cross-Encoder Reranker
 ↓
Top N
 ↓
Context Builder
 ↓
LLM
```

---

# 19. Caching Strategy

Redis will be used for:

- Query-response caching
- Rate limiting
- Temporary task information
- Worker/task coordination

Cache keys must contain tenant context.

Example:

```text
tenant:{tenant_id}:query:{hash}
```

This prevents cached responses from one organization being returned to another.

---

# 20. Background Processing

Celery workers will handle expensive operations:

```text
Document Upload
      ↓
Queue
      ↓
Celery Worker
      ↓
Parse
      ↓
Chunk
      ↓
Embed
      ↓
Qdrant Upsert
```

Failures will be recorded in PostgreSQL.

Workers should support retry policies for temporary failures.

---

# 21. Testing Strategy

## Unit Tests

Test:

- JWT generation
- Password hashing
- RBAC
- chunking
- retrieval
- prompt construction
- repository functions

## Integration Tests

Test:

- Authentication
- Document upload
- Database operations
- Qdrant integration
- Query pipeline

## Security Tests

### Tenant Isolation

```text
Create Tenant A
Create Tenant B

Upload A document
Upload B document

Query as A

Assert:
B document never appears
```

### RBAC

```text
Viewer → Upload
Expected: 403

Editor → Change User Role
Expected: 403

Viewer → Query
Expected: 200
```

## RAG Evaluation

Create a benchmark dataset containing approximately 30–50 questions.

Measure:

- Hit\@K
- MRR
- Context Precision
- Context Recall
- Faithfulness
- Answer Relevance

Compare:

```text
Dense
vs
BM25
vs
Hybrid
vs
Hybrid + Reranking
```

---

# 22. Project Milestones

## Phase 1 — Foundation

**Duration:** Week 1

Deliverables:

- Repository setup
- FastAPI application
- PostgreSQL connection
- Configuration management
- Docker development environment

---

## Phase 2 — Authentication & Multi-Tenancy

**Duration:** Weeks 2–3

Deliverables:

- Tenant registration
- User authentication
- JWT
- Refresh tokens
- RBAC
- Tenant-aware dependencies

---

## Phase 3 — Document Management

**Duration:** Weeks 4–5

Deliverables:

- File upload
- File validation
- Document database model
- Object storage
- Document listing/deletion
- Processing status

---

## Phase 4 — RAG Foundation

**Duration:** Weeks 6–7

Deliverables:

- Document parser
- Chunking
- Embeddings
- Qdrant
- Basic semantic search
- Basic answer generation
- Citations

---

## Phase 5 — Background Processing

**Duration:** Week 8

Deliverables:

- Redis
- Celery
- Ingestion queue
- Worker
- Retry handling
- Failure tracking

---

## Phase 6 — Advanced Retrieval

**Duration:** Weeks 9–10

Deliverables:

- BM25
- Hybrid retrieval
- Metadata filtering
- Cross-encoder reranking
- Role-aware retrieval

---

## Phase 7 — Production Features

**Duration:** Weeks 11–12

Deliverables:

- SSE streaming
- Redis caching
- Rate limiting
- Query history
- Usage analytics
- Audit logs

---

## Phase 8 — Testing & Evaluation

**Duration:** Weeks 13–14

Deliverables:

- Unit tests
- Integration tests
- RBAC tests
- Tenant isolation tests
- Retrieval benchmark
- RAG evaluation

---

## Phase 9 — Deployment

**Duration:** Week 15

Deliverables:

- Docker production setup
- CI/CD
- Backend deployment
- Worker deployment
- Database configuration
- Qdrant deployment
- Frontend deployment

---

## Phase 10 — Documentation & Final Presentation

**Duration:** Week 16

Deliverables:

- Final README
- Architecture documentation
- API documentation
- Evaluation results
- Demo
- Final report
- Presentation
- Project video/demo

---

# 23. Technology Stack

| Layer            | Technology                        |
| ---------------- | --------------------------------- |
| Frontend         | Next.js / React                   |
| Backend          | FastAPI                           |
| Language         | Python                            |
| API Schema       | Pydantic                          |
| Database         | PostgreSQL                        |
| ORM              | SQLAlchemy                        |
| Vector Database  | Qdrant                            |
| Cache            | Redis                             |
| Task Queue       | Celery                            |
| Embeddings       | Sentence Transformers / API model |
| Reranker         | Cross-Encoder                     |
| LLM              | Gemini / OpenAI                   |
| Authentication   | JWT                               |
| Password Hashing | Argon2/bcrypt                     |
| Containerization | Docker                            |
| CI/CD            | GitHub Actions                    |
| Deployment       | Render/Railway/VPS + Qdrant Cloud |

---

# 24. Risk Management

| Risk                           | Probability | Impact   | Mitigation                                   |
| ------------------------------ | ----------- | -------- | -------------------------------------------- |
| Poor retrieval quality         | Medium      | High     | Hybrid retrieval + reranking + evaluation    |
| Cross-tenant data leakage      | Low         | Critical | Isolation tests + tenant-scoped repositories |
| LLM hallucination              | Medium      | High     | Citations + grounded prompts + evaluation    |
| Large document processing time | Medium      | Medium   | Background workers                           |
| API abuse                      | Medium      | Medium   | Redis rate limiting                          |
| Vector DB failure              | Low         | High     | Error handling + backups                     |
| Worker failure                 | Medium      | Medium   | Retries + failed status                      |
| Prompt injection               | Medium      | High     | Treat retrieved text as untrusted            |
| Deployment issues              | Medium      | Medium   | Docker + CI/CD                               |
| Scope creep                    | High        | Medium   | Prioritize MVP features                      |

---

# 25. Success Criteria

The project will be considered successful when:

1. Multiple organizations can use the same deployed platform.
2. Users can authenticate securely.
3. RBAC is enforced.
4. Documents are processed asynchronously.
5. Documents are indexed into the vector database.
6. Users can query their organization's knowledge base.
7. Retrieved documents respect tenant and role restrictions.
8. Responses contain source citations.
9. Responses can be streamed using SSE.
10. Cross-tenant access tests pass.
11. Retrieval performance can be quantitatively evaluated.
12. The complete application can be deployed using Docker.

---

# 26. Expected Deliverables

### Software

- Web application
- FastAPI backend
- Background worker
- PostgreSQL database
- Qdrant vector database
- Redis
- Docker configuration

### Documentation

- Software Requirements Specification
- System architecture
- Database schema
- API documentation
- Security documentation
- Testing documentation
- RAG evaluation report
- Deployment guide
- Final project report

### Demonstration

The final demonstration should show:

```text
1. Register Company A
2. Register Company B
3. Upload different documents
4. Create users with different roles
5. Query Company A
6. Show citations
7. Attempt unauthorized access
8. Demonstrate access denial
9. Show asynchronous ingestion
10. Show retrieval/evaluation metrics
```

---

# 27. Expected Learning Outcomes

After completing the project, the student will gain practical experience in:

- Designing multi-tenant SaaS systems
- FastAPI application architecture
- Dependency injection
- JWT authentication
- Role-based authorization
- PostgreSQL database design
- SQLAlchemy
- Redis
- Background task processing
- Vector databases
- RAG architecture
- Hybrid information retrieval
- Reranking
- Prompt engineering
- LLM integration
- Streaming APIs
- Security testing
- Docker
- CI/CD
- Cloud deployment
- AI evaluation

---

# 28. Future Enhancements

Potential future versions may include:

- Organization-level billing
- SSO/SAML
- OAuth login
- Advanced analytics
- Multiple LLM providers
- Multi-modal document processing
- OCR for scanned documents
- Table-aware retrieval
- Knowledge graphs
- Agentic workflows
- Kubernetes deployment
- Automatic document versioning
- Enterprise audit/compliance features

---

# 29. Resume Positioning

### Project Title

**TenantMind — Multi-Tenant Enterprise AI Knowledge Platform**

### Resume Description

> Developed a multi-tenant RAG-based enterprise knowledge platform using FastAPI, PostgreSQL, Qdrant, Redis and Celery, implementing JWT authentication, RBAC, asynchronous document ingestion, hybrid retrieval, reranking and citation-grounded LLM responses.

### Stronger 3-bullet version

- Architected a **multi-tenant enterprise RAG platform** with FastAPI, PostgreSQL and Qdrant, enforcing tenant isolation and role-based document access across organizations.
- Built an **asynchronous document ingestion pipeline** using Celery and Redis for parsing, chunking, embedding and vector indexing, with hybrid dense/BM25 retrieval and cross-encoder reranking.
- Implemented **SSE-based streaming, Redis caching, citation-grounded generation and automated tenant-isolation/RBAC tests**, with retrieval and RAG quality evaluated using benchmark datasets.

Only include the technologies and metrics you actually implement.

---

# 30. Final Project Definition

**TenantMind is not primarily a chatbot.**

Its primary engineering problem is:

> **How can a shared AI platform securely serve multiple organizations while ensuring that retrieval, authorization, storage, caching, and generated responses remain isolated and grounded?**

The project therefore combines:

```text
                 TENANTMIND
                     │
       ┌─────────────┼─────────────┐
       │             │             │
       ▼             ▼             ▼
   SaaS Backend     Security       AI/RAG
       │             │             │
    FastAPI         JWT/RBAC      Retrieval
    PostgreSQL      Isolation     Reranking
    Redis           Permissions   LLM
    Celery          Audit         Citations
       │             │             │
       └─────────────┼─────────────┘
                     ▼
              Production-like
                AI Platform
```

**The main differentiator from Repix is therefore architectural rather than merely changing the document type:** Repix demonstrates RAG applied to codebases; TenantMind demonstrates how to engineer a **secure, multi-tenant AI SaaS product around RAG**.
