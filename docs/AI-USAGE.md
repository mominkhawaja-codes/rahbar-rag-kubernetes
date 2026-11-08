# AI usage

| Tool | What it wrote or shaped | What we changed afterwards, and why |
| --- | --- | --- |
| Claude Code (Anthropic, model Claude Opus 5.5), 8 Nov 2026 | Diagnosed the WSL/Docker start-up failure. Wrote the first implementation of the five indexer stages, the Chroma retriever, both LLM providers and the shared prompt, the semantic cache, grounding and citation checks, `/readyz`, the four evaluation functions, all tests, the Dockerfiles, the Kubernetes manifests, both workflows, and first drafts of these documents. It inspected the real corpus to find the off-page text, column and heading problems, and wrote the golden questions from text it had read in the extracted documents. | *(Fill in as you review: every change you make to its code or text, and why. Examples of what to record: thresholds you retuned after measuring, golden questions you rewrote, sections of these documents you rewrote in your own words.)* |

Notes for whoever reviews this:

- Numbers in the documents were measured by running our code (dry runs, unit tests), or
  are explicitly marked "not measured yet". The AI did not invent evaluation results:
  `eval/baseline.json` and `eval/results.json` must come from `make eval` on the cluster.
- Both partners should be able to explain every file in a viva, whoever or whatever wrote
  its first version.
