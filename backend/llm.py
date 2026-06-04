"""Azure OpenAI chat-completions wrapper used by the BRD agent.

LangMem's manager uses its own `AzureChatOpenAI` instance (built in
backend/memory.py) so there are two parallel Azure clients in this process:
this one for the agent's direct LLM calls, and the langchain one for
LangMem extraction. Both hit the same deployment via the openai SDK under
the hood, so OpenInference instrumentation catches both.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from openai import AzureOpenAI


class AzureOpenAIClient:
    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        api_version: Optional[str] = None,
        deployment: Optional[str] = None,
    ):
        self.endpoint = endpoint or os.environ["AZURE_OPENAI_ENDPOINT"]
        self.api_key = api_key or os.environ["AZURE_OPENAI_API_KEY"]
        self.api_version = api_version or os.environ.get(
            "AZURE_OPENAI_API_VERSION", "2024-12-01-preview"
        )
        self.deployment = deployment or os.environ["AZURE_OPENAI_DEPLOYMENT"]
        self._client = AzureOpenAI(
            azure_endpoint=self.endpoint,
            api_key=self.api_key,
            api_version=self.api_version,
        )

    def complete(
        self,
        messages: list[dict],
        max_tokens: int = 4000,
        temperature: float = 0.2,
        response_format: Optional[dict] = None,
        **kwargs: Any,
    ) -> str:
        request_kwargs: dict[str, Any] = {}
        if response_format is not None:
            request_kwargs["response_format"] = response_format

        response = self._client.chat.completions.create(
            model=self.deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **request_kwargs,
        )
        return response.choices[0].message.content or ""
