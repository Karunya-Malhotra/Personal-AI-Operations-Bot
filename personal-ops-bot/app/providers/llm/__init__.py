"""Outward adapters for language-model providers.

Only `FakeLLM` and the factory are re-exported. The vendor adapters are
deliberately *not* -- importing this package must not drag in an SDK, and the
factory imports them lazily inside its branches.
"""

from app.providers.llm.factory import KNOWN_PROVIDERS, build_llm_provider
from app.providers.llm.fake import FakeLLM

__all__ = ["KNOWN_PROVIDERS", "FakeLLM", "build_llm_provider"]
