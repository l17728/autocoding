import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from autocoding.providers import ProviderError, claude_environment, opencode_provider, parse_jsonc, resolve_zhipu


class ProviderTests(unittest.TestCase):
    def test_jsonc_preserves_url_and_comment_like_strings(self):
        text = '{/* note */"url":"https://example/x//y","text":"/*literal*/",// comment\n"array":[1,2,],}'
        self.assertEqual({"url": "https://example/x//y", "text": "/*literal*/", "array": [1, 2]},
                         parse_jsonc(text))

    def test_environment_credential_not_in_repr(self):
        config = resolve_zhipu(env={"ZHIPU_API_KEY": "TEST_SECRET_CANARY"})
        self.assertEqual("glm-5.3", config.model)
        self.assertNotIn("TEST_SECRET_CANARY", repr(config))

    def test_load_opencode_config_and_env_reference(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "opencode.jsonc"
            path.write_text('{"model":"zhipuai-coding-plan/glm-5.3","provider":'
                            '{"zhipuai-coding-plan":{"options":{"apiKey":"{env:LOCAL_KEY}"}}}}', encoding="utf-8")
            config = resolve_zhipu(path, env={"LOCAL_KEY": "test-key"})
            self.assertEqual("glm-5.3", config.model)
            self.assertEqual("test-key", config.api_key)
            self.assertEqual("env:LOCAL_KEY", config.source)

    def test_opencode_config_has_explicit_coding_plan_endpoint_and_model_metadata(self):
        config=resolve_zhipu(env={"ZHIPU_API_KEY":"PRIVATE_TEST_KEY"})
        provider=opencode_provider(config)
        self.assertEqual("@ai-sdk/openai-compatible",provider["npm"])
        self.assertEqual("https://open.bigmodel.cn/api/coding/paas/v4",provider["options"]["baseURL"])
        self.assertEqual("PRIVATE_TEST_KEY",provider["options"]["apiKey"])
        self.assertEqual({"context":1_000_000,"output":131_072},provider["models"]["glm-5.3"]["limit"])
        self.assertNotIn("PRIVATE_TEST_KEY",repr(config))

    def test_missing_key_and_unreadable_config_fail_closed(self):
        with self.assertRaisesRegex(ProviderError, "^CREDENTIAL_UNAVAILABLE$"):
            resolve_zhipu(env={})
        with self.assertRaisesRegex(ProviderError, "^CONFIG_UNREADABLE$"):
            resolve_zhipu(pathlib.Path("does-not-exist.json"), env={})

    def test_error_does_not_disclose_invalid_json_secret(self):
        with self.assertRaises(ProviderError) as error:
            parse_jsonc('{"key":"TEST_SECRET_CANARY" broken}')
        self.assertNotIn("TEST_SECRET_CANARY", str(error.exception))

    def test_anthropic_env_scoped_and_conflicting_auth_removed(self):
        parent = {"ANTHROPIC_API_KEY": "old", "CLAUDE_CODE_OAUTH_TOKEN": "old", "PATH": "example"}
        config = resolve_zhipu(env={"ZHIPU_API_KEY": "new"})
        result = claude_environment(config, "https://open.bigmodel.cn/api/anthropic", parent=parent)
        self.assertEqual("new", result["ANTHROPIC_AUTH_TOKEN"])
        self.assertNotIn("ANTHROPIC_API_KEY", result)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", result)
        self.assertEqual("old", parent["ANTHROPIC_API_KEY"])

    def test_credential_cannot_be_sent_to_wrong_host_or_protocol(self):
        config = resolve_zhipu(env={"ZHIPU_API_KEY": "secret"})
        for endpoint in [
            "http://open.bigmodel.cn/api/anthropic",
            "https://open.bigmodel.cn.attacker.test/api/anthropic",
            "https://open.bigmodel.cn/api/coding/paas/v4",
            "https://open.bigmodel.cn/api/anthropic?token=secret",
            "https://attacker@open.bigmodel.cn/api/anthropic",
        ]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ProviderError):
                claude_environment(config, endpoint, parent={})


if __name__ == "__main__":
    unittest.main()

