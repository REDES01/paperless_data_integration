# Path A integration with `paperless-ml`

This integration connects `paperless_data_integration`'s `htr_consumer` to the
integrated Paperless + ML serving stack at
[`Palomarr/paperless-ml`](https://github.com/Palomarr/paperless-ml)
(branch `paperless-integration`), while keeping
[`REDES01/paperless_data`](https://github.com/REDES01/paperless_data) as
the backing store for our HTR persistence tables
(`documents / document_pages / handwritten_regions`).

## Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│                   paperless_ml_net  (shared bridge)                 │
│                                                                     │
│  ┌──────────────┐   ┌─────────────┐   ┌──────┐   ┌────────┐         │
│  │ paperless-web│   │ ml-gateway  │   │minio │   │redpanda│         │
│  │ aliases:     │   │  alias:     │   └──────┘   └────────┘         │
│  │  webserver   │   │ fastapi_    │                                 │
│  │  paperless-  │   │  server     │        (paperless-ml stack)     │
│  │   webserver-1│   │             │                                 │
│  └──────────────┘   └─────────────┘                                 │
│                                                                     │
│              ┌────────────────┐                                     │
│              │  htr_consumer  │  (this service)                     │
│              └────────────────┘                                     │
└───────────────────────┬─────────────────────────────────────────────┘
                        │
                        │ also on
                        ▼
              ┌──────────────────────┐
              │ paperless_data_default│  (paperless_data stack's own net)
              │                      │
              │   ┌──────────┐       │
              │   │ postgres │       │
              │   └──────────┘       │
              └──────────────────────┘
```

- `htr_consumer` attaches to **both** networks:
  - `paperless_ml_net` — to reach ml-gateway, minio, redpanda, paperless-web.
  - `paperless_data_default` — to reach our own `postgres` for HTR tables.
- DNS ambiguity avoided: `paperless-ml` keeps **its** postgres off the shared
  net (only paperless-web, ml-gateway, minio, redpanda are exposed).

## Bring-up order

```bash
# 1. Shared network
../paperless_ml/scripts/create_network.sh

# 2. paperless-ml stack (integrated mode)
(cd ../paperless_ml \
 && docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d)

# 3. paperless_data stack (backing store for our HTR tables)
(cd ../paperless_data && docker compose -p paperless_data up -d)

# 4. Paperless API token — see provision_chameleon.ipynb step 16
export PAPERLESS_TOKEN=<token>

# 5. htr_consumer
docker compose -f htr_consumer/compose.yml up -d --build
```

## What changed vs `main`

| File | Change |
|---|---|
| `htr_consumer/compose.yml` | Attach to second network `paperless_data_default`. Update MinIO creds to match paperless-ml defaults (`minioadmin`/`minioadmin`). Add `HTR_ENDPOINT: /htr` to match the renamed endpoint in paperless-ml. Rewrite usage docstring for the new topology. |
| `htr_consumer/processor.py` | Read endpoint path from `HTR_ENDPOINT` env var (default `/predict/htr` for backward compat). Previously hardcoded `/predict/htr`. |
| `htr_consumer/consumer.py` | Document the new `HTR_ENDPOINT` env var in module docstring. |

Nothing else modified. `overrides/`, `region_slicer/`, `paperless/`,
`sample_documents/`, `seed/`, `scripts/`, and the `Makefile` are unchanged —
they remain available for operators who want to run the *non-integrated*
topology (the original Phase 0 network overlay pattern).

## Reverting to the pre-integration defaults

Drop the `paperless_data_default` network attachment and the
`HTR_ENDPOINT` env var override from `htr_consumer/compose.yml`, and the
defaults fall back to `main`'s behavior (hits `fastapi_server:8000/predict/htr`,
reads MinIO with `admin`/`paperless_minio`).
