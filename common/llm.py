"""LLM factory. Returns an OpenAI-compatible chat model wired to OpenRouter."""

import os

from langchain_openai import ChatOpenAI


REVIEW_POLICY = (
    "Review the PR as a senior engineer. Return a structured analysis with concrete "
    "comments and specific escalation questions. Confidence means how safe it is for "
    "the agent to post this review without more human context, not how certain you are "
    "that you spotted a defect. Use confidence below 0.58 for changes involving "
    "authentication, passwords, tokens, cryptography, SQL injection, or insecure network "
    "transport; ask 2–4 questions about the affected files. Use 0.58–0.72 when a schema "
    "change, migration, or input validation needs a reviewer decision. Reserve 0.73 or "
    "above for small, low-risk changes with no unresolved review issues."
)


def get_llm(temperature: float = 0.2) -> ChatOpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set — copy .env.example to .env")
    return ChatOpenAI(
        model=os.environ.get("LLM_MODEL", "openai/gpt-4o-mini"),
        base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=api_key,
        temperature=temperature,
        max_tokens=700,
    )
