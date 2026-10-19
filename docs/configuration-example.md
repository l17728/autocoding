# 运行配置字段示例

平台只提交非机密配置示例。下列值为设计字段，不会自动触发模型请求：

```json
{
  "schema_version": 1,
  "provider": "zhipuai-coding-plan",
  "model": "glm-5.3",
  "protocol": "anthropic-messages",
  "base_url": "REQUIRES_VERIFIED_ANTHROPIC_ENDPOINT",
  "credential_ref": "env:ZHIPU_API_KEY",
  "agent": "claude",
  "execution_mode": "protocol",
  "permissions": "native-request-flow"
}
```

base_url 必须经官方说明与连接探针核验，不能填 OpenCode 的 OpenAI 兼容地址。密钥只通过环境或可读的已授权本机配置解析，不提交实际值；保留底层 Agent 的权限控制。
