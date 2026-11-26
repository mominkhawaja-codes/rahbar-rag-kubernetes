# AI usage

| Tool | What it wrote or shaped | Human review and decisions |
| --- | --- | --- |
| Claude Code (Anthropic), 8 Nov 2026 | Diagnosed the WSL/Docker startup failure. Drafted the initial indexer stages, Chroma retriever, LLM providers and prompt, semantic cache, grounding and citation checks, readiness endpoint, evaluation functions, tests, Dockerfiles, Kubernetes manifests, workflows and project documentation. It inspected extraction issues in the public corpus and drafted golden questions from extracted text. | During later manual UI testing, a team member reproduced an incorrect cache hit for opposite CGPA comparisons and requested a guard that retained exact matches and protected answer-changing qualifiers. |
| OpenAI Codex, Assignment 02 stabilization and submission | Worked on the Groq integration, semantic cache guard and inline citation handling. Helped migrate the verified public index into a separate live collection, wrote the snapshot transfer helper and documented the two dev setup paths. | A team member chose Groq generation after observing repeated host stalls with local generation, created and verified the Kubernetes Secret, manually rolled back the experimental corpus, and reported passing five UI questions after the public migration. The team member directed that document indexing remain the default and that snapshot setup stay optional to protect the live index. |

The evaluation JSON files record the measured runs and their environments. The baseline
uses real-corpus retrieval with stub generation. The final results use Groq with the
isolated public index.
