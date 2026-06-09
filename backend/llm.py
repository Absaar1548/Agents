"""LLM provider abstraction for the BRD agent.

Supports Azure OpenAI and local Ollama via a single environment-driven
factory.  All provider-specific construction lives in this file; every
other module just imports `create_llm_client` or `build_langchain_llm`.

LangMem's manager needs a LangChain model (`AzureChatOpenAI` or
`ChatOllama`).  Direct agent nodes use the thin `BaseLLMClient` wrapper
(`AzureOpenAIClient` or `OllamaClient`) because they work with raw
OpenAI-format dicts and want simple string returns.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from openai import AzureOpenAI, OpenAI


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------
@dataclass
class BaseLLMClient:
    provider: str
    model: str

    def complete(
        self,
        messages: list[dict],
        max_tokens: int = 4000,
        temperature: float = 0.2,
        timeout: Optional[float] = 60.0,
        response_format: Optional[dict] = None,
        **kwargs: Any,
    ) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Azure OpenAI
# ---------------------------------------------------------------------------
class AzureOpenAIClient(BaseLLMClient):
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
        super().__init__(provider="azure.openai", model=self.deployment)

    def complete(
        self,
        messages: list[dict],
        max_tokens: int = 4000,
        temperature: float = 0.2,
        timeout: Optional[float] = 60.0,
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
            timeout=timeout,
            **request_kwargs,
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Local Ollama (OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------
class OllamaClient(BaseLLMClient):
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.base_url = base_url or os.environ.get(
            "OLLAMA_BASE_URL", "http://localhost:11434/v1"
        )
        self.ollama_model = model or os.environ.get("OLLAMA_MODEL", "llama3")
        self._client = OpenAI(
            base_url=self.base_url,
            api_key="ollama",
        )
        super().__init__(provider="ollama", model=self.ollama_model)

    def complete(
        self,
        messages: list[dict],
        max_tokens: int = 4000,
        temperature: float = 0.2,
        timeout: Optional[float] = 60.0,
        response_format: Optional[dict] = None,
        **kwargs: Any,
    ) -> str:
        request_kwargs: dict[str, Any] = {}
        if response_format is not None:
            request_kwargs["response_format"] = response_format

        response = self._client.chat.completions.create(
            model=self.ollama_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            **request_kwargs,
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Factory for direct agent calls
# ---------------------------------------------------------------------------
def create_llm_client() -> BaseLLMClient:
    provider = os.environ.get("LLM_PROVIDER", "azure").lower().strip()
    if provider == "ollama":
        return OllamaClient()
    if provider == "azure":
        return AzureOpenAIClient()
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r} (expected 'azure' or 'ollama')")


# ---------------------------------------------------------------------------
# Factory for LangMem (LangChain models)
# ---------------------------------------------------------------------------
def build_langchain_llm():
    """Return a LangChain chat model for LangMem.

    Azure  -> AzureChatOpenAI
    Ollama -> ChatOllama (strips trailing /v1 from base_url)
    """
    provider = os.environ.get("LLM_PROVIDER", "azure").lower().strip()
    if provider == "azure":
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(
            azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            temperature=0.0,
        )
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        # ChatOllama expects the base Ollama URL without the /v1 OpenAI suffix.
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        return ChatOllama(
            model=os.environ.get("OLLAMA_MODEL", "llama3"),
            base_url=base_url,
            temperature=0.0,
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r} (expected 'azure' or 'ollama')")
