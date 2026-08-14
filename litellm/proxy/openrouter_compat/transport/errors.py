"""Error taxonomy for OpenRouter-compatible discovery."""


class DiscoveryError(Exception):
    """Base class for discovery errors."""

    category: str = "DiscoveryError"


class DiscoveryTimeout(DiscoveryError):
    category = "DiscoveryTimeout"


class DiscoveryConnectionError(DiscoveryError):
    category = "DiscoveryConnectionError"


class DiscoveryHTTPError(DiscoveryError):
    category = "DiscoveryHTTPError"

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {detail}".strip())


class DiscoveryInvalidJSON(DiscoveryError):
    category = "DiscoveryInvalidJSON"


class DiscoverySchemaError(DiscoveryError):
    category = "DiscoverySchemaError"


class UnsupportedRuntime(DiscoveryError):
    category = "UnsupportedRuntime"


class MissingRequiredOpenRouterField(DiscoveryError):
    category = "MissingRequiredOpenRouterField"


class DiscoveryProbeError(DiscoveryError):
    """Carries an underlying error category for a single probe failure."""

    def __init__(self, probe: str, category: str, detail: str = "") -> None:
        self.probe = probe
        self.category = category
        super().__init__(f"{probe} {category}: {detail}".strip())