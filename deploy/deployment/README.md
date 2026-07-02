# VERA Deployment

Deployment scripts for all environments. Mirrors the infra structure (`infra/terraform/`).

```
deployment/
└── deployment-dev/    ← dev environment (vera-dev.techjays.com)
```

---

## Dev environment

Infrastructure: `infra/terraform/`
Scripts: `deployment/deployment-dev/`

```bash
cd deployment/deployment-dev
./deploy.sh                  # full deploy
./deploy.sh --control-plane  # redeploy API
./deploy.sh --worker         # redeploy agent
./deploy.sh --livekit        # restart livekit-server
./deploy.sh --sip            # restart livekit-sip
./deploy.sh --frontend       # build + push frontend
```

See `deployment-dev/README.md` for the full runbook.
