# PMP Phase 8 — RAG Evaluation Report (phase8-20261007-184807)


**Run timestamp:** 2026-10-07T18:48:07.197044+00:00  
**Dataset:** meridian-cloud-eval v1  
**Configurations:** dense, bm25, hybrid, hybrid_rerank  
**top_k:** 5  **candidate_limit:** 20

## 1. Executive Summary

This run measured TenantMind's retrieval pipeline on the 4 configurations of the experiment matrix. BM25 is measured against the real PostgreSQL full-text index; dense retrieval uses the documented deterministic `lexical-hash-eval-v1` embedding stand-in (no production embedding model or LLM credential is available); the reranker is `noop (cross-encoder not installed; §20 unavailable)`. The highest document-level MRR across configurations is 0.787 — see the retrieval table below. Results are for this dataset only and are not statistical significance claims.

## 2. Evaluation Dataset

Composition (questions by category):
| Category | Count |
|---|---|
| context_required | 7 |
| multi_document | 6 |
| similar_terminology | 4 |
| simple_factual | 8 |
| specific_detail | 7 |
| unanswerable | 4 |

## 3. Experimental Setup

`top_k = 5`, `candidate_limit = 20`. Each configuration runs the production retrieval components (see `evaluation/retrieval_eval.py`); the only substitutions are the clearly-labelled deterministic stand-ins below.
| Component | Used |
|---|---|
| Database | PostgreSQL (live full-text index) |
| Vector store | Qdrant |
| Embedding model | lexical-hash-eval-v1 |
| Generation model | extractive-eval-v1 |
| Reranker | noop (cross-encoder not installed; §20 unavailable) |
| Query top_k | 5 |
| Deterministic seed | n/a (all providers deterministic) |

## 4. Retrieval Metrics

| Configuration | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR | Ctx Prec | Ctx Recall |
|---|---|---|---|---|---|---|---|
| bm25 | 0.028 | 0.028 | 0.028 | 0.028 | 0.028 | 0.028 | 0.031 |
| dense | 0.722 | 0.833 | 0.889 | 0.889 | 0.787 | 0.088 | 1.000 |
| hybrid | 0.722 | 0.833 | 0.889 | 0.889 | 0.787 | 0.088 | 1.000 |
| hybrid_rerank | 0.722 | 0.833 | 0.889 | 0.889 | 0.787 | 0.088 | 1.000 |

**Level:** document-level (a document counts as retrieved if any of its chunks appears in the ranked list). Chunk-level Hit@K/MRR are in the machine-readable results; the definitions of every metric are in `evaluation/metrics.py` and summarised in the Limitations section.

## 5. Answer Metrics

| Configuration | Faithfulness | Answer Relevance | Correctness |
|---|---|---|---|
| bm25 | 1.000 | 0.004 | 0.031 |
| dense | 1.000 | 0.389 | 0.675 |
| hybrid | 1.000 | 0.389 | 0.675 |
| hybrid_rerank | 1.000 | 0.389 | 0.675 |

| Measure | Count |
|---|---|
| answer rows (answerable questions) | 128 |
| answers with correctness ≥ 0.5 | 64 |
| refusals | 31 |
| grounded answers | 97 |

## 6. Latency Results

| Configuration | Retrieval mean (ms) | Retrieval median (ms) | Generation mean (ms) | Total mean (ms) |
|---|---|---|---|---|
| bm25 | 2.231 | 2.071 | 0.001 | 2.209 |
| dense | 5.905 | 5.762 | 0.236 | 6.508 |
| hybrid | 13.269 | 11.042 | 0.226 | 11.186 |
| hybrid_rerank | 10.904 | 10.726 | 0.247 | 11.459 |

## 7. Dense vs BM25

Dense Hit@5 0.889 vs BM25 Hit@5 0.028; Dense MRR 0.787 vs BM25 MRR 0.028. The dense figure is the lexical-hash stand-in's, so this comparison measures the two matchings (cosine over hashed term distributions vs PostgreSQL ts_rank_cd) under controlled conditions — not a production bi-encoder's.

## 8. Hybrid / RRF Results

Hybrid/RRF Hit@5 0.889 vs dense 0.889 and BM25 0.028; MRR 0.787 vs 0.787 / 0.028. On this dataset RRF contributed **nothing**: BM25 matched no candidate chunks, so every hybrid row is byte-identical to the dense row and no fusion benefit was measurable. The dataset's natural-language questions are matched by ``websearch_to_tsquery`` as a conjunction of every content word, so a single vocabulary mismatch suffices to eliminate a document (see the Limitations section).

## 9. Reranker Results

Reranker `noop (cross-encoder not installed; §20 unavailable)`: Hybrid+rerank Hit@5 0.889 vs Hybrid 0.889; MRR 0.787 vs 0.787.

## 10. Failure Analysis

| Category | bm25 | dense | hybrid | hybrid_rerank |
|---|---|---|---|---|
| answer_failure | 0 | 2 | 2 | 2 |
| empty_retrieval | 31 | 0 | 0 | 0 |
| ranking_failure | 0 | 6 | 6 | 6 |
| success | 1 | 24 | 24 | 24 |
| unanswerable_context | 0 | 4 | 4 | 4 |
| unanswerable_refused | 4 | 0 | 0 | 0 |

| Query | Config | Category | Observed | Expected | Likely Cause | Recommendation |
|---|---|---|---|---|---|---|
| q001 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q002 | dense | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q002 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q002 | hybrid | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q002 | hybrid_rerank | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q003 | dense | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q003 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q003 | hybrid | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q003 | hybrid_rerank | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q004 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q006 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q007 | dense | answer_failure | the relevant document ranked first but the generated answer barely matched the reference | an answer overlapping the reference answer | a chunk-level miss (right document, wrong chunk) or the extractive stand-in's lexical scoring | re-rank at chunk level; with a real LLM, re-evaluate with a model judge |
| q007 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q007 | hybrid | answer_failure | the relevant document ranked first but the generated answer barely matched the reference | an answer overlapping the reference answer | a chunk-level miss (right document, wrong chunk) or the extractive stand-in's lexical scoring | re-rank at chunk level; with a real LLM, re-evaluate with a model judge |
| q007 | hybrid_rerank | answer_failure | the relevant document ranked first but the generated answer barely matched the reference | an answer overlapping the reference answer | a chunk-level miss (right document, wrong chunk) or the extractive stand-in's lexical scoring | re-rank at chunk level; with a real LLM, re-evaluate with a model judge |
| q008 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q009 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q010 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q011 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q012 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q013 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q014 | dense | answer_failure | the relevant document ranked first but the generated answer barely matched the reference | an answer overlapping the reference answer | a chunk-level miss (right document, wrong chunk) or the extractive stand-in's lexical scoring | re-rank at chunk level; with a real LLM, re-evaluate with a model judge |
| q014 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q014 | hybrid | answer_failure | the relevant document ranked first but the generated answer barely matched the reference | an answer overlapping the reference answer | a chunk-level miss (right document, wrong chunk) or the extractive stand-in's lexical scoring | re-rank at chunk level; with a real LLM, re-evaluate with a model judge |
| q014 | hybrid_rerank | answer_failure | the relevant document ranked first but the generated answer barely matched the reference | an answer overlapping the reference answer | a chunk-level miss (right document, wrong chunk) or the extractive stand-in's lexical scoring | re-rank at chunk level; with a real LLM, re-evaluate with a model judge |
| q015 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q016 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q017 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q018 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q019 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q020 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q021 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q022 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q023 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q024 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q025 | dense | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q025 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q025 | hybrid | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q025 | hybrid_rerank | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q026 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q027 | dense | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q027 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q027 | hybrid | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q027 | hybrid_rerank | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q028 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q029 | dense | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q029 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q029 | hybrid | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q029 | hybrid_rerank | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q030 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q031 | dense | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q031 | bm25 | unanswerable_refused | no context was retrieved, so no answer was produced | no fabricated answer | retrieved nothing to ground on | correct no-hallucination behaviour; keep |
| q031 | hybrid | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q031 | hybrid_rerank | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q032 | dense | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q032 | bm25 | unanswerable_refused | no context was retrieved, so no answer was produced | no fabricated answer | retrieved nothing to ground on | correct no-hallucination behaviour; keep |
| q032 | hybrid | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q032 | hybrid_rerank | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q033 | dense | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q033 | bm25 | unanswerable_refused | no context was retrieved, so no answer was produced | no fabricated answer | retrieved nothing to ground on | correct no-hallucination behaviour; keep |
| q033 | hybrid | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q033 | hybrid_rerank | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q034 | dense | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q034 | bm25 | unanswerable_refused | no context was retrieved, so no answer was produced | no fabricated answer | retrieved nothing to ground on | correct no-hallucination behaviour; keep |
| q034 | hybrid | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q034 | hybrid_rerank | unanswerable_context | non-relevant context was retrieved and an extractive answer was produced from it | an explicit cannot-answer response; no hallucination | retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context | with a real LLM, the grounded instructions should decline; re-verify with a model judge |
| q035 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q036 | dense | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q036 | bm25 | empty_retrieval | retrieval returned nothing for an answerable question | at least one chunk retrieved | the corpus has no chunk matching the question under this retriever's matching rules | verify the document is indexed; otherwise this is a genuine absence in the corpus |
| q036 | hybrid | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |
| q036 | hybrid_rerank | ranking_failure | a relevant document was retrieved but did not rank first | the relevant document at rank 1 | the retriever's scoring placed a non-relevant document ahead of the relevant one | investigate fusion weights and consider reranking where the reranker is available |

## 11. Limitations

* 36-question dataset: results describe this corpus only; §18 of the phase brief — no statistical significance is claimed.
* Dense retrieval used the documented deterministic `lexical-hash-eval-v1` embedding stand-in (no Gemini key, no sentence-transformers); its numbers are not a production bi-encoder's.
* Reranking is unavailable in this environment (`NoOpReranker` fallback); the Hybrid/Rerank row duplicates Hybrid/RRF, and no reranker quality or latency is claimed.
* Answer metrics were produced by the deterministic `extractive-eval-v1` provider: faithfulness reflects the grounding contract (answers are verbatim evidence sentences), not a production LLM's faithfulness.
* Context Precision/Recall are document-level (a document counts when any of its chunks is retrieved); chunk-level Hit@K/MRR are in the JSON results.
* On this dataset BM25 matched no candidate chunk for almost every question (1 of 36 hits). The production path matches each query as ``websearch_to_tsquery``'s conjunction of every content word, so a single vocabulary mismatch between the natural-language question and the document eliminates a match; hybrid/RRF therefore carried no BM25 signal and its rows are identical to dense's. This measures the real full-text index on conversational questions, and is not an ingestion or wiring failure.
* The extractive stand-in cannot decline with non-empty context, so unanswerable questions with plausible-but-irrelevant retrieval produce a grounded-but-wrong answer; the report classifies this as `unanswerable_context` rather than measuring it as hallucination.

## 12. Conclusions

1. On the Phase 8 dataset, dense achieved the higher MRR (0.787 vs 0.028); the dense figure is the lexical-hash stand-in's, so this is a matching-mode comparison, not a model one.
2. Hybrid/RRF equalled dense exactly on this dataset (MRR 0.787 vs 0.787): BM25 matched no candidate chunks, so RRF had no second ranked list to fuse and hybrid reduced to dense alone. Fusion benefit: not measurable (limitation 2).
3. Reranking could not be evaluated (no cross-encoder available); the `Hybrid/RRF + Rerank` row is the NoOp fallback and carries no reranker claim.
4. Answer relevance/correctness track retrieval quality configuration by configuration; because the answer stand-in is extractive, these numbers measure whether better retrieval delivers better grounded answers, not LLM faithfulness (which is ~1.0 by construction for the stand-in).
5. Every claim here is about this dataset and this environment; see the Limitations section before generalising.

## 13. Recommended Future Improvements

1. Run the benchmark with a production bi-encoder embedding model and a real cross-encoder reranker once credentials/models are available, and re-measure the dense and reranked configurations.
2. Re-run answer evaluation with a production LLM and a model judge; the extractive stand-in here measures the grounding contract, not generation quality.
3. Grow the dataset and, if practical, add statistical significance testing (e.g., paired bootstrap over the 36 questions) before trusting small MRR gaps.
4. Re-measure latency after the optional reranker is installed; no-op reranking latency is not representative of a cross-encoder's cost.

---

_Report generated by `backend/evaluation/runner.py` (run `phase8-20261007-184807`). Machine-readable results: `docs/evaluation/results/`._