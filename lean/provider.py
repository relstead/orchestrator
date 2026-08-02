"""
Provider client for LLM API calls.

Supports Groq and OpenRouter APIs (OpenAI-compatible).
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Message:
    """Chat message."""
    role: str  # system, user, assistant
    content: str


@dataclass
class CompletionResult:
    """Result of a completion call."""
    content: str
    model: str
    usage: dict[str, int]
    finish_reason: str
    latency_ms: float


class RateLimiter:
    """Token bucket rate limiter."""
    
    def __init__(self, requests_per_minute: float = 60, tokens_per_minute: float = 10000):
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute
        
        self._request_bucket = requests_per_minute
        self._token_bucket = tokens_per_minute
        self._last_refill = time.monotonic()
    
    def _refill(self) -> None:
        """Refill buckets."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        
        # Refill based on rate
        refill_requests = elapsed * (self.requests_per_minute / 60)
        refill_tokens = elapsed * (self.tokens_per_minute / 60)
        
        self._request_bucket = min(
            self.requests_per_minute,
            self._request_bucket + refill_requests
        )
        self._token_bucket = min(
            self.tokens_per_minute,
            self._token_bucket + refill_tokens
        )
        self._last_refill = now
    
    async def acquire(self, tokens_needed: int) -> None:
        """Acquire permission to make a request."""
        while True:
            self._refill()
            
            if self._request_bucket >= 1 and self._token_bucket >= tokens_needed:
                self._request_bucket -= 1
                self._token_bucket -= tokens_needed
                return
            
            # Wait and retry
            await asyncio.sleep(0.1)


class ProviderClient(ABC):
    """Abstract base for provider clients."""
    
    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> CompletionResult:
        """Return completion text."""
        ...


class GroqProvider(ProviderClient):
    """Groq API client."""
    
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 60.0,
        requests_per_minute: float = 30,
        tokens_per_minute: float = 6000,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        self._rate_limiter = RateLimiter(requests_per_minute, tokens_per_minute)
    
    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
    
    async def complete(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> CompletionResult:
        """Call Groq API for completion."""
        # Estimate token usage (rough approximation: 4 chars per token)
        estimated_tokens = sum(len(m.content) + len(m.role) for m in messages) // 4
        await self._rate_limiter.acquire(estimated_tokens + max_tokens)
        
        start_time = time.monotonic()
        
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        response = await self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        
        response.raise_for_status()
        data = response.json()
        
        latency_ms = (time.monotonic() - start_time) * 1000
        
        return CompletionResult(
            content=data["choices"][0]["message"]["content"],
            model=data.get("model", model),
            usage=data.get("usage", {}),
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
            latency_ms=latency_ms,
        )


class OpenRouterProvider(ProviderClient):
    """OpenRouter API client."""
    
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 120.0,
        requests_per_minute: float = 20,
        tokens_per_minute: float = 10000,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        self._rate_limiter = RateLimiter(requests_per_minute, tokens_per_minute)
    
    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
    
    async def complete(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> CompletionResult:
        """Call OpenRouter API for completion."""
        estimated_tokens = sum(len(m.content) + len(m.role) for m in messages) // 4
        await self._rate_limiter.acquire(estimated_tokens + max_tokens)
        
        start_time = time.monotonic()
        
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://vault-orchestrator.dev",
            "X-Title": "Vault Orchestrator",
        }
        
        response = await self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        
        response.raise_for_status()
        data = response.json()
        
        latency_ms = (time.monotonic() - start_time) * 1000
        
        return CompletionResult(
            content=data["choices"][0]["message"]["content"],
            model=data.get("model", model),
            usage=data.get("usage", {}),
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
            latency_ms=latency_ms,
        )


def create_provider(
    provider_type: str,
    api_key: str,
    model: str,
    base_url: str | None = None,
) -> ProviderClient:
    """Factory for creating provider clients."""
    if provider_type == "groq":
        return GroqProvider(api_key=api_key, base_url=base_url or "https://api.groq.com/openai/v1")
    elif provider_type == "openrouter":
        return OpenRouterProvider(api_key=api_key, base_url=base_url or "https://openrouter.ai/api/v1")
    elif provider_type == "openai":
        return OpenRouterProvider(api_key=api_key, base_url=base_url or "https://api.openai.com/v1")
    else:
        raise ValueError(f"Unknown provider type: {provider_type}")
