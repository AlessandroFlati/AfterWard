# AfterWard

One discharge. A conversation that continues.

AfterWard keeps discharge documents, patient questions, ward replies and follow-ups in one case. The ward can prepare and deliver documents before the patient leaves; a patient can also start with a PDF. Google Gemma transcribes scanned pages through the AMD development API, with the original always available for human review.

Built for the [AMD Developer Hackathon Act III](https://lablab.ai/ai-hackathons/amd-developer-hackathon-act-iii). This repository contains the runnable product, example PDFs and a small offline regression suite. Private documents, credentials, development notebooks, interview answers, bulk evaluation corpora and experiment logs are excluded.

## Try the product

Run locally using the instructions below, then open <http://127.0.0.1:8776/judge-guide>. The app includes three guided cases with actions and expected outcomes:

1. Ask about a missing rehabilitation sheet; share the existing document, send selected replies, continue a follow-up in the same thread and resolve it as the patient.
2. Start from the ward, review the discharge bundle and explicitly deliver it without making the patient upload it again.
3. Upload the supplied scanned PDF, inspect real Gemma OCR and compare the transcription with the source page.

Use **Connect another device** to join the same case from another browser. An invite works once and expires after ten minutes. Starting a new demo creates a separate case.

The `/presentation` page contains the video walkthrough and a six-page outline for the PDF pitch. These are preparation materials, not a finished submission. The production URL still serving the earlier release must not be presented as this version until deployment is completed.

## Current scope

| Feature | Status in this interface |
| --- | --- |
| PDF originals, page rendering and reviewed text | Implemented |
| Gemma-4-31B-IT image transcription on AMD | Implemented; API opt-in, reasoning `none` for OCR |
| Persistent patient/ward conversations and linked follow-ups | Implemented |
| Per-question reminders and selected batch replies | Implemented |
| Explicit document delivery and patient-controlled resolution | Implemented |
| Shared case across browsers/devices | Implemented with demo identities |
| Automatic gap detection, plain-language explanations, ward AI drafting | Qualification work exists separately; not connected to this web interface |
| FHIR input | Bounded library/CLI foundation; no live hospital connector |
| Verified hospital identity, real delegation, external notifications | Not implemented |
| Clinical outcomes, production security certification, measured staff savings | Not established |

The interface never substitutes a recorded answer for a failed API call. Staff replies in the guided cases are written by the person using the staff view. OCR transcription does not establish clinical correctness.

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

### Enable real AMD OCR

Use only invented, non-sensitive documents with the shared development endpoint. Its [terms](https://amd-aim.github.io/radeon-cloud-docs/api/terms/) exclude personal, health and other confidential data and describe request logging. This trial endpoint is not a production healthcare data-processing service.

Set these environment variables before starting the app. `.env.example` documents configuration; the portable launcher does not automatically load `.env`.

| Variable | Value / meaning |
| --- | --- |
| `AMD_TOKEN_FACTORY_API_KEY` | Server-side AMD token; never commit or expose it in the browser |
| `AFTERWARD_AMD_ENABLED` | `1` to enable OCR |
| `AFTERWARD_AMD_FREE_QUOTA_CONFIRMED` | `1` after verifying the account's free entitlement |
| `AFTERWARD_API_EXPIRES` | Explicit final UTC date, `YYYY-MM-DD` |
| `AFTERWARD_API_TOTAL_LIMIT` | Total attempts for this deployment; default `200` |
| `AFTERWARD_API_DAILY_LIMIT` | Attempts per UTC day; default `100` |

The fixed endpoint is `https://developer.amd.com.cn/radeon/api/v1`; model `Gemma-4-31B-IT`. The browser sends PDF bytes to this server. The server renders pages locally, checks `/usage`, reserves an attempt in SQLite, then sends a PNG to AMD. OCR uses `reasoning_effort=none`, temperature `0`, a bounded JSON response and no automatic retry. This setting applies to transcription, not to the still-separate document-analysis experiments.

Each attempt, including failed inference, counts. Requests are serialized and spaced. The client preserves the final $0.05-equivalent daily quota allocation reported by AMD; the API's quota units are not evidence of a paid retail price. Changing environment limits does not reset or silently expand a persisted ledger. A leftover lease after an interrupted process blocks further inference until an operator verifies that no request is active and clears only that lease.

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
- `gemma_ocr.py`: fixed AMD provider, response validation and quota accounting.
- `app.py`: portable launcher and persistent deployment configuration.
- `foundation/`, `handoff.py`, `evidence.py`, `documents.py`: scoped FHIR/evidence foundations and local extraction.
- `web/`, `assets/demo/`: self-hosted interface, judge guide, presentation walkthrough and invented PDFs.

Hosted mode accepts only the exact packaged demonstration PDFs. There is no public path for personal records. Demo participants can switch between roles: these identities are not hospital authentication. Data is not encrypted by the application; Railway infrastructure controls do not by themselves establish GDPR compliance. Deploy one replica with a persistent volume. Do not use this release for clinical decisions or actual patient care.

## Verification

```powershell
python -m unittest discover -s product/tests_release -v
```

The portable offline suite checks shared sessions and deletion, linked follow-ups, selective reminders, delivery boundaries, CSRF/revision checks, scan handling, API output and quota failures, hosted routes and the public PDF allowlist. It requires no AMD key. It is not an accuracy benchmark or clinical validation.

For fixture regeneration, install the optional dependencies with `python -m pip install "./product[fixtures]"`, then run `python product/scripts/build_workspace_pdfs.py`.

## Deployment and provenance

See [DEPLOYMENT.md](DEPLOYMENT.md) for the concrete Railway configuration, verification and rollback procedure. The Docker build contains only runtime sources and assets, never `.env`, private files or the research workspace.

Selected deterministic FHIR extraction, schema and Merkle audit foundations were adapted from the author's pre-existing [TrustedRisk](https://github.com/AlessandroFlati/TrustedRisk), commit `d79bcf5a8c289a4a2f51989608b9db8d33a21efc`, under MIT. [product/provenance.json](product/provenance.json) records the exact sources and adaptations. This reuse is disclosed; original ownership does not itself determine competition eligibility. Hashes support integrity checks, not proof of identity or clinical truth.

Application code is MIT licensed. Bundled fonts retain their respective license files. No claims of direct Instinct, ROCm, Quark or AITER execution are made by this release.
