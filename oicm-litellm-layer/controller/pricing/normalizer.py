import re

_SUFFIX_PATTERN = re.compile(
    r"-(?:instruct|turbo|tput|fp8|fp16|bf16|awq|gguf|v1|v2|001|002)"
    r"|(?:-\d{4}-\d{2}-\d{2})"
    r"|(?::\d+)$"
    r"|(?:-\d{6,})$",
    flags=re.IGNORECASE,
)

_DASH_COLLAPSE = re.compile(r"-+")


def normalize_model_name(raw: str) -> str:
    if not raw:
        return ""

    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]

    if "." in raw:
        parts = raw.split(".", 1)
        prefix, rest = parts[0], parts[1]
        if prefix.isalpha() and "-" in rest:
            raw = rest

    raw = raw.lower()

    prev: str | None = None
    while prev != raw:
        prev = raw
        raw = _SUFFIX_PATTERN.sub("", raw)

    raw = raw.replace("_", "-").replace(" ", "-")
    raw = _DASH_COLLAPSE.sub("-", raw)
    return raw.strip("-")


def parse_family_and_params(normalized: str) -> tuple[str, str | None]:
    if not normalized:
        return ("", None)

    matches = list(re.finditer(r"(\d+(?:\.\d+)?b)\b", normalized))
    if len(matches) != 1:
        return (normalized, None)

    match = matches[0]
    param_count = match.group(1)
    cut = normalized[: match.start()].rstrip("-")
    family = cut if cut else normalized
    return (family, param_count)


def tokenize(normalized: str) -> tuple[str, ...]:
    if not normalized:
        return ()
    return tuple(tok for tok in normalized.split("-") if tok)
