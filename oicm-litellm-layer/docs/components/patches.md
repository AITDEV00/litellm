# Patches

Fork patches applied against the upstream LiteLLM codebase. These are the
minimal changes this layer makes on top of unmodified upstream LiteLLM.

## Files

| File | Purpose |
|------|---------|
| `patches/embedding-extra-body.patch` | ~5-line patch adding `extra_body` merge to the hosted vLLM embedding transformation |

## Where the patch target lives upstream

The patch modifies `litellm/llms/hosted_vllm/embedding/transformation.py` in the
main repo (`../` relative to the layer). See the `README.md` architecture table
(component #6) for the full list of fork points.