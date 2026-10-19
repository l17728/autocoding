"""Resolve an explicitly authorized local Zhipu credential without persisting it."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    model: str
    source: str
    api_key: str = field(repr=False)


def parse_jsonc(text: str):
    """Remove comments/trailing commas only outside JSON strings."""
    result = []
    i, quoted, escaped = 0, False, False
    while i < len(text):
        ch = text[i]
        if quoted:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            i += 1
            continue
        if ch == '"':
            quoted = True
            result.append(ch)
            i += 1
        elif text[i:i+2] == "//":
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
        elif text[i:i+2] == "/*":
            end = text.find("*/", i + 2)
            if end == -1:
                raise ProviderError("CONFIG_INVALID")
            result.append(" ")
            i = end + 2
        else:
            result.append(ch)
            i += 1
    # Second pass: commas separated from a close bracket by whitespace.
    text = "".join(result)
    result, i, quoted, escaped = [], 0, False, False
    while i < len(text):
        ch = text[i]
        if quoted:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
            result.append(ch)
        elif ch == ",":
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j >= len(text) or text[j] not in "]}":
                result.append(ch)
        else:
            result.append(ch)
        i += 1
    try:
        data = json.loads("".join(result).lstrip("\ufeff"))
    except (ValueError, TypeError):
        raise ProviderError("CONFIG_INVALID") from None
    if not isinstance(data, dict):
        raise ProviderError("CONFIG_INVALID")
    return data


def resolve_zhipu(config_path: Path | None = None, *, env=None, model_override=None):
    env = os.environ if env is None else env
    data = {}
    if config_path is not None:
        try:
            data = parse_jsonc(Path(config_path).read_text(encoding="utf-8-sig"))
        except OSError:
            raise ProviderError("CONFIG_UNREADABLE") from None
    selected = data.get("model", "")
    model = model_override
    if not model and isinstance(selected, str) and selected.startswith("zhipuai-coding-plan/"):
        model = selected.split("/", 1)[1]
    model = model or "glm-5.3"  # Observed local selection; endpoint acceptance still requires a live probe.
    if not isinstance(model, str) or not model.startswith("glm-") or any(c.isspace() for c in model):
        raise ProviderError("MODEL_INVALID")
    key = env.get("ZHIPU_API_KEY")
    source = "env:ZHIPU_API_KEY"
    if not key:
        try:
            provider = data.get("provider", {}).get("zhipuai-coding-plan", {})
            value = provider.get("options", {}).get("apiKey")
        except AttributeError:
            raise ProviderError("CONFIG_INVALID") from None
        if isinstance(value, str) and value.startswith("{env:") and value.endswith("}"):
            name = value[5:-1]
            key = env.get(name)
            source = "env:" + name
        elif isinstance(value, str) and not value.startswith("{"):
            key = value
            source = "opencode:provider.options.apiKey"
    if not isinstance(key, str) or not key.strip() or any(c in key for c in "\r\n\0"):
        raise ProviderError("CREDENTIAL_UNAVAILABLE")
    return ProviderConfig(model=model, source=source, api_key=key.strip())


def claude_environment(config: ProviderConfig, base_url: str, *, parent=None):
    """Accept only the explicitly chosen domestic HTTPS Anthropic endpoint.

    Allowlisting prevents accidental disclosure to a mistyped host; it does
    not claim this endpoint/model has been independently verified.
    """
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise ProviderError("ENDPOINT_NOT_ALLOWED") from None
    if (parsed.scheme != "https" or parsed.hostname != "open.bigmodel.cn"
            or parsed.username or parsed.password or port not in (None, 443)
            or parsed.path.rstrip("/") != "/api/anthropic" or parsed.query or parsed.fragment):
        raise ProviderError("ENDPOINT_NOT_ALLOWED")
    result = dict(os.environ if parent is None else parent)
    # Do not let an unrelated inherited Anthropic account compete with the chosen provider.
    for name in ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"]:
        result.pop(name, None)
    result.update({
        "ANTHROPIC_BASE_URL": base_url.rstrip("/"),
        "ANTHROPIC_AUTH_TOKEN": config.api_key,
        "ANTHROPIC_MODEL": config.model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": config.model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": config.model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": config.model,
    })
    return result


