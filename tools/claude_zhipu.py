"""Inspect a local Zhipu configuration or launch Claude with process-scoped settings.

Uses the domestic endpoint documented by MetaGLM. Live model/key compatibility
still requires verification; --check never sends a key.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autocoding.providers import ProviderError, claude_environment, resolve_zhipu
from probe_agents import executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, help="Authorized OpenCode JSON/JSONC config file")
    parser.add_argument("--model", help="Defaults to OpenCode selection or observed glm-5.3")
    parser.add_argument("--base-url", default="https://open.bigmodel.cn/api/anthropic", help="Explicitly chosen domestic Anthropic-compatible HTTPS endpoint")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Resolve metadata only; never launch or print key")
    mode.add_argument("--run", action="store_true", help="Launch interactive Claude in this terminal")
    args = parser.parse_args()
    try:
        local_config = ROOT / ".local/credentials/opencode.json"
        config_path = args.config
        if config_path is None and local_config.exists():
            config_path = local_config
        config = resolve_zhipu(config_path, model_override=args.model)
        binary = executable("claude")
        if not binary:
            raise ProviderError("CLAUDE_NOT_INSTALLED")
        if not args.run:
            print(json.dumps({"status": "credential_resolved", "agent": "claude", "model": config.model,
                              "credential_source": config.source, "key_printed": False,
                              "endpoint_documented": True, "live_connection_verified": False},
                             ensure_ascii=False, indent=2))
            return 0
        env = claude_environment(config, args.base_url)
        # No --dangerously-skip-permissions, no credentials on argv, no global settings write.
        return subprocess.call([binary, "--model", config.model], cwd=ROOT, env=env)
    except ProviderError as exc:
        print(json.dumps({"status": "blocked", "code": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

