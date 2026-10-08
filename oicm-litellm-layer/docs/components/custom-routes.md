# Custom Routes

Custom route plugins for the LiteLLM proxy, registered via the proxy's
pass-through / custom route extension points.

The OICM voice routes (`/v1/audio/speech/clone`, `/v1/audio/voices`,
`/v1/voices`, `/v1/voices/profiles` CRUD, `/v1/audio/script`, `/v1/audio/models`,
`/v1/audio/health`, `/v1/audio/metrics`) live in the LiteLLM source tree as a
vertical slice: `litellm/proxy/voice_routes.py` (mounted on the app from
`proxy_server.py`). See [OICM Custom Code](../oicm-slices.md).

## Files

| File | Purpose |
|------|---------|
| `litellm/proxy/voice_routes.py` | The voice/audio route slice (implementation lives in the litellm source tree, not this layer) |
| `docs/custom-routes-plans/CLONE-LOGIC-MAP.md` | Logic map of the clone route |
| `docs/custom-routes-plans/VSA-PLAN.md` | Vertical-slice architecture plan for the custom routes |

## Docs

- `docs/custom-providers/` — provider-specific research and audits (HAMSA,
  INCEPTION, OMNIVOICE) that these custom routes serve
  - `LITELLM_ENDPOINT_ARCHITECTURE.md`
  - `HAMSA_TTS_BEHAVIOR.md`
  - `HAMSA_RESEARCH.md`
- `docs/usage-guides/` — how to call the routes through the gateway
  - `GATEWAY_GUIDE.md`
  - `INCEPTION_TTS_STT_GUIDE.md`
  - `OMNIVOICE_TTS_GUIDE.md`