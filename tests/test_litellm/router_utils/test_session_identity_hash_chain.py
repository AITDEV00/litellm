"""Hash-chain and declared-id tests, ported from llm-d producer/chunk tests."""

from litellm.router_utils.session_identity.hash_chain import (
    chain_continuation,
    chunk_chain,
    declared_id,
    root_seed,
    shared_prefix_len,
    synthesized_session_id,
)

SEED = root_seed("model-a", "")


def test_chain_stable_when_turn_appended():
    """Appending a turn must not move earlier chunk boundaries."""
    turn1 = ("system prompt. " * 200).encode()
    turn1_plus_2 = turn1 + ("user question " * 200).encode()
    chain1 = chunk_chain(turn1, SEED, chunk_size=512, max_chunks=64)
    chain2 = chunk_chain(turn1_plus_2, SEED, chunk_size=512, max_chunks=64)
    assert chain1
    assert chain2[: len(chain1)] == chain1


def test_chain_empty_below_min_size():
    """A stream too small for one full chunk + reserve yields no lineage."""
    assert chunk_chain(b"tiny", SEED, chunk_size=512, max_chunks=64) == []
    # exactly chunk_size but under the +reserve requirement: still nothing
    assert chunk_chain(b"x" * 512, SEED, chunk_size=512, max_chunks=64) == []


def test_chain_detects_divergence():
    """A different suffix shares only its prefix of hashes."""
    base = ("shared history " * 300).encode()
    fork_a = base + b"alpha continuation " * 100
    fork_b = base + b"beta continuation " * 100
    chain_a = chunk_chain(fork_a, SEED, chunk_size=512, max_chunks=64)
    chain_b = chunk_chain(fork_b, SEED, chunk_size=512, max_chunks=64)
    assert shared_prefix_len(chain_a, chain_b) < len(chain_a)
    assert shared_prefix_len(chain_a, chain_a) == len(chain_a)


def test_chain_ignores_divergence_in_partial_chunk():
    """A suffix too small to fill a chunk is dropped with the partial chunk:
    the two forks stay identical until their difference completes a chunk."""
    base = ("shared history " * 300).encode()
    fork_a = base + b"alpha"
    fork_b = base + b"beta"
    chain_a = chunk_chain(fork_a, SEED, chunk_size=512, max_chunks=64)
    chain_b = chunk_chain(fork_b, SEED, chunk_size=512, max_chunks=64)
    assert chain_a == chain_b


def test_chain_utf8_safe_boundaries():
    """Multi-byte runes must not be split differently as the stream grows."""
    base = (" système français " * 100).encode()  # multibyte chars
    grown = base + " nouvelle question".encode()
    chain1 = chunk_chain(base, SEED, chunk_size=64, max_chunks=64)
    chain2 = chunk_chain(grown, SEED, chunk_size=64, max_chunks=64)
    assert chain1
    assert chain2[: len(chain1)] == chain1


def test_chain_byte_identical_prefixes_match_regardless_of_sender():
    """Same bytes + same seed = same chain; seed scopes model/salt."""
    stream = b"same content " * 100
    assert chunk_chain(stream, SEED, 512, 64) == chunk_chain(stream, SEED, 512, 64)
    other_model = root_seed("model-b", "")
    assert chunk_chain(stream, SEED, 512, 64) != chunk_chain(stream, other_model, 512, 64)


def test_continuation_extends_one_lineage():
    """Continuing from the prior chain's last hash keeps one lineage."""
    prior_stream = ("remembered history " * 200).encode()
    prior = chunk_chain(prior_stream, SEED, 512, 64)
    new_turn = b"only the newest turn " * 100
    extended = chain_continuation(new_turn, prior, SEED, 512, 64)
    assert extended[: len(prior)] == prior
    assert len(extended) > len(prior)
    # resending the prior stream again must reproduce the remembered prefix
    resent = chunk_chain(prior_stream, SEED, 512, 64)
    assert resent == prior


def test_continuation_caps_at_max_chunks():
    prior = ["a" * 64] * 64
    extended = chain_continuation(b"more " * 100, prior, SEED, 512, 64)
    assert len(extended) == 64


def test_declared_id_namespaced():
    assert declared_id({"prompt_cache_key": "abc"}) == "prompt_cache_key\x00abc"
    assert declared_id({"conversation": "conv-1"}) == "conversation\x00conv-1"
    assert declared_id({"prompt_cache_key": ""}) is None
    assert declared_id({"prompt_cache_key": 123}) is None
    assert declared_id({}) is None
    # priority: prompt_cache_key wins
    assert (
        declared_id({"prompt_cache_key": "a", "conversation": "b"})
        == "prompt_cache_key\x00a"
    )


def test_declared_id_long_value_distinct():
    """Two long ids sharing a prefix must not collapse onto one lineage."""
    long_a = "x" * 300 + "a"
    long_b = "x" * 300 + "b"
    id_a = declared_id({"conversation": long_a})
    id_b = declared_id({"conversation": long_b})
    assert id_a is not None and id_b is not None
    assert id_a != id_b


def test_synthesized_session_id_stable_and_scoped():
    chain = ["h1", "h2", "h3"]
    sid = synthesized_session_id(chain, "model-a", "scope-1")
    assert sid == synthesized_session_id(list(chain), "model-a", "scope-1")
    assert sid != synthesized_session_id(chain[:-1], "model-a", "scope-1")
    assert sid != synthesized_session_id(chain, "model-b", "scope-1")
    assert sid != synthesized_session_id(chain, "model-a", "scope-2")
