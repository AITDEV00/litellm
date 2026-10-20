"""Shadow conftest for the client_distribution test dir.

The parent ``tests/test_litellm/proxy/conftest.py`` imports ``proxy_server``,
which transitively imports the ``openrouter`` SDK. That SDK's API changed and
is incompatible in this venv, making every test under ``proxy/`` fail at
collection. This shadow conftest prevents pytest from walking up to the
parent's conftest for this directory only.
"""
