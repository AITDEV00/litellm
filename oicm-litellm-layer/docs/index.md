# OICM → LiteLLM Layer — Quick Navigator

This site is the **entry point** for anyone (human or agent) working in the
`oicm-litellm-layer/` directory. Its job is simple: tell you **where things are**
so you know exactly which file to open and edit for a given task.

!!! tip "How to use this"
    Start from the **Structure** page for the full directory map. If your task
    is about credentials, passwords, or the master key, go straight to
    **Credentials & Secrets** — that is the page that prevents the exact kind
    of incident where one component is updated and another is forgotten.

## One-line summary

| Task you want to do | Where to go |
|---------------------|-------------|
| Change the admin password / master key | [Credentials & Secrets](credentials.md) |
| Understand the directory layout | [Structure](structure.md) |
| Edit the discovery controller logic | [Discovery Controller](components/controller.md) |
| Edit proxy / LiteLLM config | [Config](components/config.md) |
| Deploy / apply / rollout to the cluster | [Deployment & Cluster](deployment.md) |
| Find an existing doc | [Docs Map](docs-map.md) |

## Layout

```
oicm-litellm-layer/
├── mkdocs.yml          ← this site's config (edit to add nav pages)
├── docs/               ← this documentation's source markdown
├── controller/         ← discovery controller (component #1)
├── config/             ← LiteLLM proxy configs
├── hooks/              ← LiteLLM callbacks / hooks (components #3, #4)
├── custom-routes/      ← custom route plugins
├── patches/            ← fork patches against upstream litellm
├── deploy/             ← Kubernetes manifests (apply these)
└── ...                 ← full map on the Structure page
```