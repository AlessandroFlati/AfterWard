# AfterWard

One discharge. A conversation that continues.

AfterWard helps patients understand discharge documents and helps wards identify information to clarify before and after discharge. Google Gemma, through the AMD API, reads scans, checks missing or conflicting instructions, locates answers before a patient sends a question, and prepares plain-language explanations and optional ward replies. Questions that still need people continue in a shared conversation. Original sources remain available for review.

Built for the [AMD Developer Hackathon Act III](https://lablab.ai/ai-hackathons/amd-developer-hackathon-act-iii). This repository contains the runnable product, example PDFs and a small offline regression suite. Private documents, credentials, development notebooks, interview answers, bulk evaluation corpora and experiment logs are excluded.

## Try the product

Open the [live demo](https://afterward-demo.up.railway.app/) and its [hands-on guide](https://afterward-demo.up.railway.app/judge-guide), or run locally using the instructions below. The app includes three guided entry routes with actions and expected outcomes:

1. Ask about a missing rehabilitation sheet; share the existing document, send selected replies, continue a follow-up in the same thread and resolve it as the patient.
2. Start from the ward, review the discharge bundle and explicitly deliver it without making the patient upload it again.
3. Upload the supplied scanned PDF, inspect real Gemma OCR and compare the transcription with the source page.

Use **Connect another device** to join the same case from another browser. An invite works once and expires after ten minutes. Starting a new demo creates a separate case.

The `/presentation` page contains the product narrative and video walkthrough. The public host runs this English workspace with live AMD inference and accepts only the packaged example PDFs. A recorded video and the final competition submission remain separate deliverables.

## Current scope

| Feature | Status in this interface |
| --- | --- |
| PDF originals, page rendering and reviewed text | Implemented |
| Gemma-4-31B-IT image transcription on AMD | Implemented; API opt-in, reasoning `none` for OCR |
| Persistent patient/ward conversations and linked follow-ups | Implemented |
| Per-question reminders and selected batch replies | Implemented |
| Explicit document delivery and patient-controlled resolution | Implemented |
| Shared case across browsers/devices | Implemented with demo identities |
| Automatic gap detection, plain-language explanations, ward AI drafting | Connected: medium document checks, cached clinical glossary and ward drafting; explanations appear directly as AI-generated |
| Patient question check before contacting the ward | Connected: medium reasoning over patient-visible documents, exact source excerpts, explicit selection before sending; the patient can bypass the check |
| FHIR input | Bounded library/CLI foundation; no live hospital connector |
| Verified hospital identity, real delegation, external notifications | Not implemented |
| Clinical outcomes, production security certification, measured staff savings | Not established |

The interface never substitutes a recorded answer for a failed API call. Staff can write directly or review an AI suggestion; publication is always explicit. OCR transcription does not establish clinical correctness.

Staff can upload and review a PDF from a reply editor, return to that conversation with their draft retained, and explicitly share selected documents. Reply suggestions use the documents selected in that editor. Model names, reasoning levels and request metrics are retained in server evidence rather than routine patient and ward screens.

When a requested attachment is already in the ward case file, the reply model can recommend sharing that document. The server requires a citation to an available, undelivered document; staff review and confirm the recipient and selected files. A suggestion never publishes a document or claims delivery by itself.

Patients can ask free-form explanation questions alongside an exact, unique extract from a document they can access. Gemma receives the question and that document's complete reviewed text. It can explain general terminology and units, while missing clinical information is referred to the ward and unrelated requests are declined. Source revision and extract checks run before inference; answer checks reject unsupported numbers and verbatim non-answers. These controls do not establish semantic correctness or guarantee resistance to prompt injection. Questions and AI explanations are saved together, and contacting the ward remains a separate explicit action.

## Run locally

Python 3.11 or later is required. The container uses Python 3.13. No JavaScript build step or external font CDN is needed.

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install -r product/requirements-hosted.txt
python -m pip install --no-deps ./product
python -m carerelay.app
```

Open <http://127.0.0.1:8776/>. The default launch performs no model inference. Selectable PDF text is extracted; a scan requires manual transcription while API OCR is disabled. Session data is written to `.afterward-data/`, which is ignored by Git. Sessions expire after twelve hours.

On Linux/macOS, activate the environment with `source .venv/bin/activate` and use the same Python commands. The internal Python namespace remains `carerelay` to retain compatibility with the imported foundation; the product and distribution are named AfterWard.

### Enable real AMD inference

Use only invented, non-sensitive documents with the shared development endpoint. Its [terms](https://amd-aim.github.io/radeon-cloud-docs/api/terms/) exclude personal, health and other confidential data and describe request logging. This trial endpoint is not a production healthcare data-processing service.

Set these environment variables before starting the app. `.env.example` documents configuration; the portable launcher does not automatically load `.env`.

| Variable | Value / meaning |
| --- | --- |
| `AMD_TOKEN_FACTORY_API_KEY` | Server-side AMD token; never commit or expose it in the browser |
| `AFTERWARD_AMD_ENABLED` | `1` to enable OCR, text tasks and background clinical explanations |
| `AFTERWARD_AMD_FREE_QUOTA_CONFIRMED` | `1` after verifying the account's free entitlement |
| `AFTERWARD_API_EXPIRES` | Explicit final UTC date, `YYYY-MM-DD` |
| `AFTERWARD_API_TOTAL_LIMIT` | Total attempts for this deployment; default `200` |
| `AFTERWARD_API_DAILY_LIMIT` | Attempts per UTC day; default `100` |

The fixed endpoint is `https://developer.amd.com.cn/radeon/api/v1`; model `Gemma-4-31B-IT`. The browser sends PDF bytes to this server. The server renders pages locally, checks `/usage`, reserves an attempt in SQLite, then sends a PNG to AMD. OCR uses `reasoning_effort=none`, temperature `0`, a bounded JSON response and no automatic retry. Document checking uses medium reasoning: facts, findings with a contrary-fact audit, and a conditional conflict pass. At most one completed schema-invalid response is corrected. Ward drafts use medium. Free-form document questions use none with the question and complete reviewed document. Patients do not have to copy an extract: the model selects a supporting passage, which must match the visible document uniquely before an answer is accepted; explanations appear directly beside the source as AI-generated; staff review is optional. When the model flags insufficient context, its explanation is withheld and the patient can explicitly prepare and send a clarification question. Text tasks run asynchronously, with source-revision invalidation and explicit failures. Each check accepts up to five selected pages and 60,000 characters. Structured runtime logs separate application queue time, quota requests and inference requests without logging document text or credentials.

Clinical wording is selected and explained with medium reasoning in the background after the last OCR page, before transcription confirmation. Existing documents are prepared when their case is opened. A shared SQLite cache on the persistent server volume keys results by exact document text, model, effort and prompt version. Identical documents across sessions reuse one pending or completed task; corrected text and prompt changes create a new key. Opening a prepared term makes no inference request. Cache reads follow document visibility: a patient cannot access an undelivered ward document's explanation. Failed tasks require an explicit retry; interrupted tasks resume on access. The non-sensitive demo glossary cache is retained for up to seven days and pruned at service startup; personal local mode never uses this cache or the AMD API. Definitions are source-bound AI output, not clinical validation. The prepared term list appears first. A separate free-form document question uses none reasoning and remains available below it when the precomputed selection does not cover a concern. Source-free refusals may be saved, but substantive answers require a validated source passage.

Each attempt, including background preparation and failed inference, counts. Requests are serialized and spaced. The client preserves the final $0.05-equivalent daily quota allocation reported by AMD; the API's quota units are not evidence of a paid retail price. Changing environment limits does not reset or silently expand a persisted ledger. A leftover lease after an interrupted process blocks further inference until an operator verifies that no request is active and clears only that lease.

HTTP 429 responses retain a whitelisted limiter code and the provider's Retry-After delay in diagnostic logs, without response bodies. The running client blocks further attempts until that delay expires; this cooldown is in memory. Failed jobs retain their retry time so reply editors can show a countdown and an explicit retry button. No automatic inference retry is made.

Latency logs correlate `ai_task`, `amd_attempt` and `amd_request` through task/attempt IDs. They separate app queueing, ledger access, pacing, quota checks, DNS/TCP connection, TLS/proxy tunnel, sending, waiting for complete response headers, response body reading and JSON parsing. Connection time contains DNS/TCP and TLS/tunnel time; do not add the parent and child measurements together. Header wait is not time to first generated token: non-streaming requests cannot distinguish upstream queueing from generation or network delay. Logs retain numeric sizes/timings and allowlisted opaque request IDs, never source text, prompts, output bodies or credentials. Certificate verification and redirect rejection remain enabled.

To repeat the same medium Case 1 checks through the deployed application's normal session and queue, run `python product/scripts/measure_railway_latency.py --samples 8 --output build/railway-latency.json`. Use the existing Python environment and a new output path for each series. This makes counted live calls within existing free-quota controls, runs serially, and stops on the first failure without retrying it. The JSON report retains every attempt, token counts and server timings, plus client observation time with three-second polling. It reports minimum/median/maximum for successful requests separately from failure counts; a small series does not estimate a reliable p95. Cookies stay in memory. Keep measured reports private and out of the public source archive.

## Architecture

```text
Browser (English, dark theme)
  -> HTTP host/origin/CSRF boundary
  -> SQLite workspace + per-question conversation state
  -> PDFium rendering -> AMD Gemma API -> strict transcription validation
  -> source comparison and explicit human acceptance
```

- `product/src/carerelay/discharge_flow.py`: revision checks, role authorization, threads, reminders and patient resolution.
- `discharge_service.py`: durable sessions, one-use invites, document visibility and intake assets.
- `discharge_http.py`: bounded same-origin HTTP adapter, fixed static routes and `/health`.
- `gemma_ocr.py`: fixed AMD provider, response validation and shared quota accounting.
- `discharge_ai.py`, `document_checks.py`, `assets/prompts/`: product adaptation of the v7 two-pass checks, exact source resolution, conditional conflict comparison, explanations, patient question prechecks and optional reply drafting.
- `app.py`: portable launcher and persistent deployment configuration.
- `foundation/`, `handoff.py`, `evidence.py`, `documents.py`: scoped FHIR/evidence foundations and local extraction.
- `web/`, `assets/demo/`: self-hosted interface, judge guide, presentation walkthrough and invented PDFs.

Hosted mode accepts only the exact packaged demonstration PDFs. There is no public path for personal records. Demo participants can switch between roles: these identities are not hospital authentication. Data is not encrypted by the application; Railway infrastructure controls do not by themselves establish GDPR compliance. Deploy one replica with a persistent volume. Do not use this release for clinical decisions or actual patient care.

## Verification

```powershell
python -m unittest discover -s product/tests_release -v
```

The portable offline suite checks shared sessions and deletion, linked follow-ups, selective reminders, delivery boundaries, CSRF/revision checks, scan handling, API output and quota failures, hosted routes and the public PDF allowlist. It also checks asynchronous jobs, scope isolation, changed-source rejection, bounded correction and human publication gates. It requires no AMD key. It is not an accuracy benchmark or clinical validation.

For fixture regeneration, install the optional dependencies with `python -m pip install "./product[fixtures]"`, then run `python product/scripts/build_workspace_pdfs.py`. This rebuilds the three simple documents and preserves the separately reconstructed three-page raster OCR example.

## Deployment and provenance

See [DEPLOYMENT.md](DEPLOYMENT.md) for the concrete Railway configuration, verification and rollback procedure. The Docker build contains only runtime sources and assets, never `.env`, private files or the research workspace.

Selected deterministic FHIR extraction, schema and Merkle audit foundations were adapted from the author's pre-existing [TrustedRisk](https://github.com/AlessandroFlati/TrustedRisk), commit `d79bcf5a8c289a4a2f51989608b9db8d33a21efc`, under MIT. [product/provenance.json](product/provenance.json) records the exact sources and adaptations. This reuse is disclosed; original ownership does not itself determine competition eligibility. Hashes support integrity checks, not proof of identity or clinical truth.

Application code is MIT licensed. Bundled fonts retain their respective license files. No claims of direct Instinct, ROCm, Quark or AITER execution are made by this release.
