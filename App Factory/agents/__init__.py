"""Agent prompt assets.

This package holds no model bindings and no vendor names. Each role owns a
directory with a `system.md` file; `prompts.py` loads those files and assembles
the per-call prompt. Swapping providers or models is a `config/models.toml`
edit and never touches anything in here.
"""

from .prompts import PromptError, PromptLibrary, RenderedPrompt

__all__ = ["PromptError", "PromptLibrary", "RenderedPrompt"]
