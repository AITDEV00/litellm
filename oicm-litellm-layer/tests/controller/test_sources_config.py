"""Tests for the declarative OICM source config and source builder.

Pins behavior, not structure: the config must reject a malformed document rather
than silently produce an empty source list (which would make the controller read
every deployment as deleted), apply env overrides, and skip a source whose
credentials are missing.
"""

import textwrap

import pytest

from controller.sources_config import OicmSourceConfig, load_sources, parse_sources
from controller.status_sources import build_status_sources


def _document(extra: str = "") -> str:
    return textwrap.dedent(
        f"""
        sources:
          - name: alain
            base_url: http://alain.local
            auth_url: http://alain-auth.local
            workspace_id: ws-alain
          - name: abudhabi
            base_url: http://ad.local
            auth_url: http://ad-auth.local
            workspace_id: ws-ad
            timeout: 30
            concurrency: 5
        {extra}
        """
    )


def _write(tmp_path, text):
    path = tmp_path / "sources.yaml"
    path.write_text(text)
    return str(path)


class TestParse:
    def test_parses_multiple_sources_with_defaults(self):
        import yaml

        sources = parse_sources(yaml.safe_load(_document()))

        assert [s.name for s in sources] == ["alain", "abudhabi"]
        assert sources[0].realm == "adeo"
        assert sources[0].client_id == "adeo"
        assert sources[0].grant_type == "password"
        assert sources[0].verify_tls is True
        # Per-source overrides are honored.
        assert sources[1].timeout == 30
        assert sources[1].concurrency == 5

    def test_missing_required_field_raises(self):
        import yaml

        with pytest.raises(ValueError, match="workspace_id"):
            parse_sources(yaml.safe_load(_document().replace("workspace_id: ws-ad", "")))

    def test_empty_source_list_raises(self):
        # An empty list must be loud: the controller would otherwise treat every
        # deployment as deleted.
        import yaml

        with pytest.raises(ValueError, match="at least one source"):
            parse_sources(yaml.safe_load("sources: []"))

    def test_duplicate_name_raises(self):
        import yaml

        dup = "sources:\n  - name: x\n    base_url: a\n    auth_url: b\n    workspace_id: c\n  - name: x\n    base_url: d\n    auth_url: e\n    workspace_id: f\n"
        with pytest.raises(ValueError, match="duplicate"):
            parse_sources(yaml.safe_load(dup))


class TestCluster:
    """The cluster a source serves is separate from the source's name.

    A source is named for what it is; the cluster says where its deployments run.
    A model row records the cluster, and that is how a consumer finds the right
    heartbeat, so the cluster names must be distinct across sources.
    """

    def test_cluster_defaults_to_the_source_name(self):
        import yaml

        sources = parse_sources(yaml.safe_load(_document()))

        assert [s.cluster for s in sources] == ["alain", "abudhabi"]

    def test_cluster_can_differ_from_the_source_name(self):
        """A source named for its role can still declare its real cluster."""
        import yaml

        doc = """
        sources:
          - name: primary-oicm
            cluster: alain
            base_url: http://a
            auth_url: http://b
            workspace_id: c
        """

        sources = parse_sources(yaml.safe_load(doc))

        assert sources[0].name == "primary-oicm"
        assert sources[0].cluster == "alain"

    def test_two_sources_sharing_a_cluster_raises(self):
        """An ambiguous cluster would make the heartbeat lookup wrong."""
        import yaml

        doc = """
        sources:
          - name: a
            cluster: shared
            base_url: http://a
            auth_url: http://b
            workspace_id: c
          - name: b
            cluster: shared
            base_url: http://d
            auth_url: http://e
            workspace_id: f
        """

        with pytest.raises(ValueError, match="distinct clusters"):
            parse_sources(yaml.safe_load(doc))


class TestEnvOverrides:
    def test_override_rewrites_only_the_named_source(self):
        import yaml

        sources = parse_sources(yaml.safe_load(_document()))
        ad = next(s for s in sources if s.name == "abudhabi")

        overridden = ad.with_env_overrides(
            {"OICM_SOURCE_ABUDHABI_BASE_URL": "http://moved.local"}
        )

        assert overridden.base_url == "http://moved.local"
        assert overridden.workspace_id == "ws-ad"
        assert overridden.name == "abudhabi"

    def test_env_prefix_sanitizes_non_alphanumerics(self):
        cfg = OicmSourceConfig(
            name="abudhabi-2", base_url="b", auth_url="a", workspace_id="w"
        )
        assert cfg.env_prefix() == "OICM_SOURCE_ABUDHABI_2"

    def test_verify_tls_can_be_overridden(self):
        import yaml

        alain = parse_sources(yaml.safe_load(_document()))[0]
        assert alain.with_env_overrides({"OICM_SOURCE_ALAIN_VERIFY_TLS": "false"}).verify_tls is False


class TestLoad:
    def test_loads_from_file_and_applies_env(self, tmp_path):
        path = _write(tmp_path, _document())

        sources = load_sources(path, env={"OICM_SOURCE_ALAIN_REALM": "other"})

        assert sources[0].realm == "other"
        assert sources[1].realm == "adeo"

    def test_missing_file_returns_empty_not_raise(self, tmp_path):
        assert load_sources(str(tmp_path / "nope.yaml"), env={}) == ()

    def test_configmap_wrapper_is_unwrapped(self, tmp_path):
        """The committed file is applied as a ConfigMap and read locally.

        One file serves both roles, so a local run must unwrap the ConfigMap
        rather than needing a second copy that can drift.
        """
        wrapped = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: oicm-sources\n"
            "data:\n  sources.yaml: |\n"
            + textwrap.indent(_document(), "    ")
        )
        path = _write(tmp_path, wrapped)

        sources = load_sources(path, env={})

        assert [s.name for s in sources] == ["alain", "abudhabi"]
        assert sources[0].base_url == "http://alain.local"

    def test_repository_sources_file_is_valid(self):
        """The committed ConfigMap must parse and name both clusters."""
        from controller.sources_config import _LOCAL_SOURCES_FILE

        sources = load_sources(str(_LOCAL_SOURCES_FILE), env={})

        assert [s.name for s in sources] == ["alain", "abudhabi"]
        assert sources[0].workspace_id != sources[1].workspace_id


class TestBuildStatusSources:
    def test_builds_one_source_per_config_with_credentials(self, tmp_path):
        path = _write(tmp_path, _document())
        configs = load_sources(path, env={})
        env = {
            "OICM_SOURCE_ALAIN_USERNAME": "u1",
            "OICM_SOURCE_ALAIN_PASSWORD": "p1",
            "OICM_SOURCE_ABUDHABI_USERNAME": "u2",
            "OICM_SOURCE_ABUDHABI_PASSWORD": "p2",
        }

        sources = build_status_sources(env=env, configs=configs)

        assert [s.name for s in sources] == ["alain", "abudhabi"]
        assert [s.workspace_id for s in sources] == ["ws-alain", "ws-ad"]

    def test_source_without_credentials_is_skipped(self, tmp_path):
        """A partial rollout degrades to fewer sources rather than failing."""
        path = _write(tmp_path, _document())
        configs = load_sources(path, env={})
        env = {
            "OICM_SOURCE_ALAIN_USERNAME": "u1",
            "OICM_SOURCE_ALAIN_PASSWORD": "p1",
            # abudhabi credentials absent
        }

        sources = build_status_sources(env=env, configs=configs)

        assert [s.name for s in sources] == ["alain"]

    def test_env_overrides_preserve_the_cluster(self):
        """An override must not blank the cluster.

        `with_env_overrides` rebuilds the config field by field, so a field it
        forgets is silently replaced by the dataclass default. Dropping the
        cluster that way would make every row's cluster empty in production
        while tests passed, because the override path is what the Deployment
        actually runs.
        """
        import yaml

        sources = parse_sources(yaml.safe_load(_document()))

        overridden = tuple(s.with_env_overrides({}) for s in sources)

        assert [s.cluster for s in overridden] == ["alain", "abudhabi"]

    def test_env_can_override_the_cluster(self):
        import yaml

        sources = parse_sources(yaml.safe_load(_document()))
        overridden = sources[0].with_env_overrides({"OICM_SOURCE_ALAIN_CLUSTER": "alain-prod"})

        assert overridden.cluster == "alain-prod"
