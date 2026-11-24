# vLLM 0.20.0 — Onboarding Bundle

This bundle registers **vLLM 0.20.0** (vLLM) on your OICM platform.

| Field | Value |
|-------|-------|
| Name | vLLM |
| Version | 0.20.0 |
| Family | vLLM |
| Devices | nvidia |
| Task Types | Text Generation, Image-Text-to-Text, Text Embedding, Text Ranking, Automatic Speech Recognition |

---

## Prerequisites

The following tools must be installed and available on your `PATH`:

- `bash` 3.2 or newer — the scripts use bash-only syntax and will fail to run
  under a POSIX shell such as `dash` or `sh`
- [`curl`](https://curl.se/) — for making HTTP requests
- [`jq`](https://jqlang.github.io/jq/) — for JSON processing
- `uuidgen` — for generating UUIDs (pre-installed on macOS and most Linux distros)

---

## Setup

### 1. Configure credentials

Copy `example.env` to `.env` and fill in your platform credentials:

```bash
cp example.env .env
```

Open `.env` and set:

```bash
OICM_HOST="https://oicm.your-domain.ai"
AUTH_BASE_URL="https://auth.your-domain.ai"
APPLICATION_ADMIN_USERNAME="your-username"
APPLICATION_ADMIN_PASSWORD="your-password"
```

All other values are pre-filled and do not need to be changed.

### 2. Make scripts executable (if needed)

```bash
chmod +x onboard.sh offboard.sh
```

---

## Onboard the Model Server

```bash
./onboard.sh              # Interactive — prompts for confirmation before sending
./onboard.sh --dry-run    # Preview the request payload without sending
./onboard.sh -d           # Same as --dry-run
```

## Args File

`config.json` defines the server arguments, inference arguments and supported model
architectures for this model server.
It is referenced automatically by `onboard.sh` — no changes needed.


---

## Remove the Model Server

```bash
./offboard.sh                    # Interactive — lists servers, select to delete
./offboard.sh --list             # List all registered model servers
./offboard.sh --id <server-id>   # Delete a specific server by ID
```

