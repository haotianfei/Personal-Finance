"""advisor LLM 网关：OpenAI 兼容 `/chat/completions`（复用 httpx，无新依赖）。

- 只返回结构化 JSON dict；坏 JSON/HTTP 错误统一 `AdvisorLlmError`。
- 不记录 prompt 全文、api_key、base_url（spec §6.4）。
- 测试一律注入 fake gateway；本模块不在测试中发真实网络请求。
"""
import json
import re

import httpx

PROMPT_VERSION_FOR_CLIENT = "advisor.prompt.v1"
_TIMEOUT_S = 45.0


class AdvisorLlmError(Exception):
    """LLM 调用/解析失败（网络、超时、非 2xx、坏 JSON）。"""


def _endpoint(base_url: str) -> str:
    return (base_url or "").rstrip("/") + "/chat/completions"


def _parse_json(content: str) -> dict:
    text = (content or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text:
        raise AdvisorLlmError("模型返回空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdvisorLlmError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise AdvisorLlmError("模型输出不是 JSON 对象")
    return data


class HttpLlmGateway:
    """`LlmGateway` 实现（provider 为明文行快照）。"""

    def __init__(self, provider: dict):
        self.provider = dict(provider or {})

    def complete_json(self, *, prompt: str, schema: dict,
                      model: str | None = None) -> dict:
        body = {
            "model": model or self.provider.get("model"),
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        kind = self.provider.get("kind")
        if kind == "deepseek":
            body["thinking"] = {"type": "disabled"}
        elif kind == "dashscope":
            body["enable_thinking"] = False
        headers = {
            "Authorization": f"Bearer {self.provider.get('api_key') or ''}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=_TIMEOUT_S) as client:
                resp = client.post(_endpoint(self.provider.get("base_url")),
                                   headers=headers, json=body)
            if resp.status_code // 100 != 2:
                raise AdvisorLlmError(f"LLM HTTP {resp.status_code}")
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
        except AdvisorLlmError:
            raise
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise AdvisorLlmError(f"LLM 调用失败: {exc}") from exc
        return _parse_json(content)