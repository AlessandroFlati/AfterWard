# Railway deployment

This deploys the English workspace and guided cases from this repository. The existing public host now runs this workspace. Keep the existing service, domain, volume and workspace spending limits.

## Configuration

Use service `afterward-demo`, environment `production`, with the existing volume mounted at `/data`. Set the source root to the repository root; use the root `Dockerfile` and `railway.json`. Do not run `railway up .` from the private research workspace. Deploy the public repository or a clean export of its committed tree.

The deployment is one replica. It uses `/data/afterward-workspace/workspaces.sqlite3` and `/data/afterward-workspace/ocr-budget.sqlite3`. These new names preserve the older service's files. The new schema does not import old demo cases.

| Variable | Required value |
| --- | --- |
| `AFTERWARD_HOSTED` | `1` (also set by the image) |
| `AFTERWARD_DATA_DIR` | `/data/afterward-workspace` |
| `AFTERWARD_PUBLIC_HOST` | `afterward-demo.up.railway.app` (hostname only) |
| `PORT` | Let Railway inject it; configure the generated domain to target that port |
| `AFTERWARD_AMD_ENABLED` | `1` for live OCR, document checks, question prechecks, explanations and reply drafts; `0` for workflow only |
| `AMD_TOKEN_FACTORY_API_KEY` | Existing secret, entered in Railway Variables; never put it in Git or a build argument |
| `AFTERWARD_AMD_FREE_QUOTA_CONFIRMED` | `1` only while the free account entitlement is verified |
| `AFTERWARD_API_EXPIRES` | Last intended demonstration date in UTC, for example `2026-10-25` for this event |
| `AFTERWARD_API_TOTAL_LIMIT` | `200` |
| `AFTERWARD_API_DAILY_LIMIT` | `100` |

`RAILWAY_PUBLIC_DOMAIN` is supported if no explicit public host is set. Old `AFTERWARD_TRUSTED_HOST` and model-release flags from the previous application are not read by this entry point. The key fallback `AFTERWARD_AMD_API_KEY` is supported.

Keep the configured workspace soft limit of $7 and hard limit of $10, and Railway agent limit of $0. These are separate from the AMD API quota. This guide does not change or increase them.

Railway [detects the Dockerfile](https://docs.railway.com/builds/dockerfiles). The app binds `0.0.0.0:$PORT`; `/health` accepts Railway's `healthcheck.railway.app` host only for that health route, as required by the [healthcheck documentation](https://docs.railway.com/deployments/healthchecks). Other routes remain restricted to the configured public domain and loopback.

## Publish a clean revision

The preferred source is the public GitHub repository. Select this repository in the service settings and deploy the intended commit. Verify the service's actual Start Command is `python -m carerelay.app`, with healthcheck path `/health` and timeout `60`. The first migration build succeeded but the existing service retained its old `carerelay.railway` override and failed at startup; correcting the service settings resolved that deployment failure. Do not infer runtime settings from a successful Docker build alone.

This existing service uses legacy `railway.json` configuration. Railway currently documents that legacy config-as-code continues until December 1, 2026; a later infrastructure migration should follow the [current configuration documentation](https://docs.railway.com/config-as-code). No infrastructure migration is needed to redeploy this event's existing service.

If using the already authenticated CLI instead:

```powershell
$env:RAILWAY_NO_TELEMETRY='1'
git clone --depth 1 https://github.com/AlessandroFlati/LabLabAMD.git AfterWard-release
Set-Location AfterWard-release
npx --yes @railway/cli@5.68.0 link
npx --yes @railway/cli@5.68.0 status
npx --yes @railway/cli@5.68.0 up . --path-as-root --service afterward-demo
```

During `link`, select the existing project, `production` and `afterward-demo`. Check those names before the upload. The clean checkout excludes the inaccessible Linux-venv links that caused the earlier upload error.

## Verify before sharing with judges

1. `/health` returns `200` with application `AfterWard`.
2. `/api/config` shows `ocr_provider: amd_api`, `ai_enabled: true` and `packaged_uploads_only: true` for the live OCR demo.
3. `/judge-guide` and `/presentation` are English and all four example PDF downloads work.
4. Complete the guided missing-handout check, source-linked question, optional AI reply, follow-up and patient resolution. Verify that unreviewed explanations and unsent reply drafts stay private.
5. Open a one-use invite in another browser and confirm the same case code and two-way replies.
6. Upload the three-page reconstructed scan and inspect actual Gemma results on every page. Accept reviewed text, then run documentary analysis. A successful healthcheck alone does not prove AMD access.
7. Restart the service and verify that the active case and quota counts persist. Do not delete the quota database to refresh allocation.

The source image can be checked locally without enabling API calls:

```powershell
docker build -t afterward:release .
docker run --rm -p 8780:8080 --mount type=volume,source=afterward-release-data,target=/data -e PORT=8080 -e AFTERWARD_PUBLIC_HOST=localhost:8780 afterward:release
```

The public upload restriction is enforced before PDF parsing or inference. The same four packaged PDFs are available from `/examples/`. Their hashes are computed from shipped assets; do not replace the files in a running release without rebuilding it.

## Rollback and retained data

Use Railway's deployment history to redeploy the previous successful image. Leave `/data` attached. The new application uses its own subdirectory, so the previous demo's files remain available to the previous image. Railway warns that services with volumes can have a short interruption during redeployment.

This is a bounded hackathon demonstration, not a hospital production deployment. Identity integration, a suitable healthcare inference provider and data-processing agreement, operational security review and clinical validation remain separate gates.
