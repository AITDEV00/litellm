# Patches

Fork patches applied against the upstream LiteLLM codebase. These are the
minimal changes this layer makes on top of unmodified upstream LiteLLM.

## Files

No active patches. `patches/embedding-extra-body.patch` is retained as a
historical artifact only.

## Superseded: embedding extra_body

`embedding-extra-body.patch` (v1.92.0 era) taught the hosted vLLM embedding
transformation to merge `extra_body` into the request so vLLM-specific params
like `truncate_prompt_tokens` reached the server. Upstream has since reworked
`litellm/llms/hosted_vllm/embedding/transformation.py` and `extra_body` now
flows through `get_optional_params_embeddings` and is flattened into the
request body natively. Do not apply the patch; it no longer applies and its
behavior is covered by upstream.

If a future fork patch is needed, add it here with the target file, the
upstream version it was written against, and the commit that proved it
applied.

## Where the patch target lived upstream

The retired patch targeted `litellm/llms/hosted_vllm/embedding/transformation.py`
in the main repo (`../` relative to the layer).