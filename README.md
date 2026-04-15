# Paperless-ngx ML Data Integration

**Team:** Dongting Gao (Training), Yikai Sun (Serving), Elnath Zhao (Data)

Deployment and integration layer that glues the Paperless-ngx UI fork, the ML serving layer, and the data platform into a single runnable system. This repo owns **Phase 0** of the integration plan: putting both compose stacks on a shared Docker network so containers in one can reach containers in the other by service name.

## Sibling repos

This repo does not contain source code for either the Paperless UI or the data platform. It expects both to be cloned as siblings:

| Repo | Purpose |
|---|---|
| [`paperless_data`](https://github.com/REDES01/paperless_data) | Data platform — PostgreSQL, MinIO, Redpanda, Qdrant, ingestion pipelines, batch pipeline |
| `paperless-ngx-fork` | Paperless-ngx Angular/Django fork with the HTR review + semantic search UI pages |
| `paperless_data_integration` | **(this repo)** — shared network, compose overrides, Chameleon provisioning |

Expected layout on disk:

```
<workspace>/
├── paperless_data/                (cloned from paperless_data repo)
├── paperless-ngx-fork/            (cloned from UI fork repo)
└── paperless_data_integration/    (this repo)
```

## What Phase 0 does

Both the Paperless stack and the data stack each bring up their own Docker Compose project with their own default network. Before Phase 0, containers in one stack cannot resolve or reach containers in the other. After Phase 0, every service is additionally attached to a user-defined bridge network named `paperless_ml_net`, so Paperless's Django code can reach `postgres:5432`, `qdrant:6333`, `redpanda:9092`, and `minio:9000` in the data stack by name.

This is purely a network-topology change. Neither stack's internal behavior is modified.

## Repo layout

```
paperless_data_integration/
├── README.md                       ← you are here
├── provision_chameleon.ipynb       ← Chameleon deployment notebook
├── Makefile                        ← convenience targets for the VM
├── paperless/
│   ├── docker-compose.yml          ← Paperless stack compose (builds from ../../paperless-ngx-fork)
│   └── docker-compose.env.example  ← env template with PAPERLESS_SECRET_KEY placeholder
├── overrides/
│   ├── paperless.override.yml      ← joins Paperless services to paperless_ml_net
│   └── paperless_data.override.yml ← joins data-stack services to paperless_ml_net
└── scripts/
    ├── create_network.sh           ← idempotent docker network create
    ├── up_paperless.sh             ← docker compose up with override
    ├── up_paperless_data.sh        ← same for the data stack
    ├── verify.sh                   ← cross-stack DNS check
    └── down.sh                     ← stop both stacks
```

## Quick start — Chameleon (target deployment)

Open `provision_chameleon.ipynb` in the Chameleon Jupyter environment and run cells top to bottom. The notebook:

1. Reserves an `m1.xlarge` VM on KVM@TACC for 12 hours
2. Assigns a floating IP and opens security groups for all service ports
3. Installs Docker
4. Clones all three repos into `~/`
5. Creates the shared `paperless_ml_net` network
6. Generates a `PAPERLESS_SECRET_KEY` and writes `paperless/docker-compose.env`
7. Builds the Paperless custom image from the fork (slow on first run, ~10–15 min)
8. Brings up the data stack with the override
9. Brings up the Paperless stack with the override
10. Verifies cross-stack DNS works
11. Prints access URLs

## Quick start — local dev

Assumes Docker Desktop is running and all three repos are cloned as siblings on your machine.

```bash
cd paperless_data_integration
./scripts/create_network.sh
./scripts/up_paperless_data.sh
./scripts/up_paperless.sh
./scripts/verify.sh
```

### Windows note

Windows reserves several TCP port ranges for Hyper-V (see `netsh int ipv4 show excludedportrange protocol=tcp`). The port ranges that commonly catch deployment are **8911–9010** (MinIO's default 9000/9001) and **50000–50059**. If you hit a port bind error, remap the conflicting port to a high number (e.g. `19000:9000`) in the corresponding compose file. The data team's repo already ships with MinIO on `19000`/`19001` for this reason.

## Verification

After everything is up, `scripts/verify.sh` runs `getent hosts <target>` from inside `paperless-webserver-1` against `postgres`, `minio`, `redpanda`, and `qdrant`. If every target resolves to a private IP, Phase 0 is complete.

## What's next (not in this repo yet)

Phase 0 is only the network plumbing. Later phases are tracked in the integration plan document and include:

- Phase 1 — Django ML views in Paperless that write to the data stack's Postgres
- Phase 2 — HTR preprocessing service that consumes `paperless.uploads` events
- Phase 3 — Document indexing service that upserts chunks into Qdrant
- Phase 4 — `/ml-api` proxy from Paperless to Yikai's FastAPI serving
- Phase 5 — Document ID bridging between ML UUIDs and Paperless integer IDs
- Phase 6 — Repoint the data generator from the stub API to real Paperless endpoints
