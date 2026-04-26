"""Unified context budget management for LLM agent calls.

General-purpose module — no knowledge of specific agent roles, data types,
or business logic. Callers provide Section objects with content and priority;
this module ensures the assembled prompt fits within the token budget.

Three strategies when context exceeds budget (applied in order):
1. LLM summarization of lowest-priority compressible sections
2. Smart truncation (heading-aware, preserves structure)
3. Hard truncate (safety net, should rarely fire)

Usage:
    from dataline.core.context_manager import ContextManager, Section

    cm = ContextManager(token_limit=262_144)
    sections = [
        Section("question", question_text, priority=100, compressible=False),
        Section("domain_rules", rules_text, priority=70),
        Section("data_profile", profile_text, priority=50),
    ]
    prompt = cm.assemble(sections, llm=my_llm)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .token_estimator import estimate_tokens

logger = logging.getLogger(__name__)

# Budget parameters
_USABLE_FRACTION = 0.70     # Reserve 30% for output + system overhead
_OUTPUT_RESERVE = 8_000     # Tokens reserved for LLM response
_SUMMARIZE_TARGET = 0.40    # Summarize to 40% of original size


@dataclass(frozen=True)
class Section:
    """A named content block to include in the LLM prompt.

    Callers create sections with appropriate priorities. ContextManager
    compresses lowest-priority sections first when budget is exceeded.

    Attributes:
        name: Identifier for logging/debugging.
        content: Text content of this section.
        priority: Higher = more important = compressed last. 100 = never compress.
        compressible: If False, this section is never compressed or truncated.
        fixed_tokens: Non-zero for content with fixed token cost (e.g., images).
            These tokens are subtracted from budget before allocating to text.
        heading: Optional markdown heading to prepend when rendering.
    """
    name: str
    content: str
    priority: int = 50
    compressible: bool = True
    fixed_tokens: int = 0
    heading: str = ""


class ContextManager:
    """Assembles prompt sections within a token budget.

    Does not know about agent roles, workspace, or state types.
    Callers collect sections from whatever source they use.
    """

    def __init__(self, token_limit: int):
        """Initialize with the API's token limit (e.g., 262_144 for Kimi k2.5).

        The usable budget is computed as:
            token_limit * 0.70 - output_reserve
        """
        self._token_limit = token_limit
        self._budget = max(int(token_limit * _USABLE_FRACTION) - _OUTPUT_RESERVE, 1)

    @property
    def budget_tokens(self) -> int:
        """Total usable token budget for prompt content."""
        return self._budget

    @property
    def token_limit(self) -> int:
        """Raw API token limit."""
        return self._token_limit

    def assemble(
        self,
        sections: list[Section],
        llm: object | None = None,
    ) -> str:
        """Assemble sections into a single prompt string within budget.

        Args:
            sections: Content sections with priorities. Order is preserved
                in output (sections are NOT reordered by priority).
                Sections are NOT mutated — internal copies are used.
            llm: Optional LLM client for summarization. If None, falls back
                to truncation when over budget. Must have a .chat() method.

        Returns:
            Assembled prompt string that fits within token budget.
        """
        # Filter out empty sections
        active = [s for s in sections if s.content and s.content.strip()]

        if not active:
            return ""

        # Subtract fixed-token sections from budget
        fixed_cost = sum(s.fixed_tokens for s in active)
        text_budget = self._budget - fixed_cost

        if text_budget <= 0:
            logger.warning(
                "Fixed-token sections (%d) exceed budget (%d). "
                "Rendering without compression.",
                fixed_cost, self._budget,
            )
            return _render(active)

        # Estimate total text tokens
        total_tokens = _total_text_tokens(active)

        # Fast path: everything fits
        if total_tokens <= text_budget:
            logger.debug(
                "Context fits: %d tokens ≤ %d budget", total_tokens, text_budget,
            )
            return _render(active)

        logger.info(
            "Context over budget: %d tokens > %d budget. Compressing...",
            total_tokens, text_budget,
        )

        # Work on mutable copies to avoid mutating caller's sections
        working = list(active)  # shallow copy of list
        # Map from original index to current content (overrides)
        content_overrides: dict[int, str] = {}

        # Compress: iterate through compressible sections by priority (low first)
        compressible_indices = sorted(
            [i for i, s in enumerate(working) if s.compressible],
            key=lambda i: working[i].priority,
        )

        for idx in compressible_indices:
            section = working[idx]
            current_content = content_overrides.get(idx, section.content)
            section_tokens = estimate_tokens(current_content)
            if section_tokens == 0:
                continue

            target_tokens = _compression_target(section_tokens, total_tokens, text_budget)

            if target_tokens >= section_tokens:
                continue  # This section doesn't need compression

            # Try LLM summarization first
            if llm is not None:
                compressed = _llm_summarize(section, current_content, target_tokens, llm)
                if compressed is not None:
                    new_tokens = estimate_tokens(compressed)
                    # Guard: only accept if summary is actually smaller
                    if new_tokens < section_tokens:
                        content_overrides[idx] = compressed
                        total_tokens = total_tokens - section_tokens + new_tokens
                        logger.info(
                            "Summarized '%s': %d → %d tokens",
                            section.name, section_tokens, new_tokens,
                        )
                        if total_tokens <= text_budget:
                            break
                        continue
                    else:
                        logger.debug(
                            "LLM summary for '%s' not smaller (%d >= %d), "
                            "falling back to truncation",
                            section.name, new_tokens, section_tokens,
                        )

            # Fallback: smart truncation
            truncated = _smart_truncate(current_content, target_tokens)
            new_tokens = estimate_tokens(truncated)
            content_overrides[idx] = truncated
            total_tokens = total_tokens - section_tokens + new_tokens
            logger.info(
                "Truncated '%s': %d → %d tokens", section.name, section_tokens, new_tokens,
            )
            if total_tokens <= text_budget:
                break

        # Safety net: if still over budget after all compression
        if total_tokens > text_budget:
            logger.warning(
                "Still over budget after compression: %d > %d. "
                "Applying hard truncate on lowest-priority section.",
                total_tokens, text_budget,
            )
            _hard_truncate_lowest(working, content_overrides, total_tokens, text_budget)

        return _render_with_overrides(working, content_overrides)

    def estimate_total(self, sections: list[Section]) -> int:
        """Estimate total tokens for a list of sections (no compression)."""
        active = [s for s in sections if s.content and s.content.strip()]
        return _total_text_tokens(active) + sum(s.fixed_tokens for s in active)


# --- Internal helpers ---


def _total_text_tokens(sections: list[Section]) -> int:
    """Sum estimated tokens for all text content in sections."""
    return sum(estimate_tokens(s.content) for s in sections)


def _compression_target(
    section_tokens: int, total_tokens: int, budget: int,
) -> int:
    """Compute how many tokens a section should be compressed to.

    Strategy: each section should shrink proportionally to the overall
    overshoot, but never below 40% of its original size.
    """
    if total_tokens <= budget:
        return section_tokens  # No compression needed

    ratio = budget / total_tokens  # e.g., 0.6 means we need to cut 40%
    target = int(section_tokens * ratio)
    minimum = int(section_tokens * _SUMMARIZE_TARGET)
    return max(target, minimum)


_CHUNK_CHAR_LIMIT = 50_000  # Max chars per LLM summarization call


def _llm_summarize(
    section: Section, content: str, target_tokens: int, llm: object,
) -> str | None:
    """Use LLM to summarize a section to fit within target_tokens.

    For content > _CHUNK_CHAR_LIMIT chars, splits by headings, summarizes
    each chunk independently, and merges results.

    Returns None if summarization fails or LLM is unavailable.
    """
    if len(content) > _CHUNK_CHAR_LIMIT:
        return _llm_summarize_chunked(section, content, target_tokens, llm)
    return _llm_summarize_single(section.name, content, target_tokens, llm)


def _llm_summarize_single(
    name: str, content: str, target_tokens: int, llm: object,
) -> str | None:
    """Summarize a single chunk via LLM."""
    try:
        target_chars = target_tokens * 4
        prompt = (
            f"Summarize the following content to approximately {target_chars} characters. "
            f"Preserve ALL exact numbers, formulas, field names, and definitions. "
            f"Remove narrative filler and redundant descriptions. "
            f"Use structured markdown (headings, bullet points). "
            f"Never paraphrase formulas — copy them exactly.\n\n"
            f"--- CONTENT TO SUMMARIZE ---\n{content}"
        )
        result = llm.chat(prompt, "Summarize now. Be concise but preserve all factual content.")  # type: ignore[union-attr]
        if result and len(result.strip()) > 20:
            return result.strip()
    except Exception as exc:
        logger.debug("LLM summarization failed for '%s': %s", name, exc)
    return None


def _llm_summarize_chunked(
    section: Section, content: str, target_tokens: int, llm: object,
) -> str | None:
    """Split large content by headings, summarize each chunk, merge."""
    # Split by H1-H3 headings
    parts = re.split(r"(?=^#{1,3}\s)", content, flags=re.MULTILINE)
    parts = [p for p in parts if p.strip()]

    # Merge small parts into chunks under the limit
    chunks: list[str] = []
    current = ""
    for part in parts:
        if len(current) + len(part) > _CHUNK_CHAR_LIMIT and current:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)

    # Fallback: hard split if single chunk still too large
    final_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) > _CHUNK_CHAR_LIMIT:
            for i in range(0, len(chunk), _CHUNK_CHAR_LIMIT):
                final_chunks.append(chunk[i:i + _CHUNK_CHAR_LIMIT])
        else:
            final_chunks.append(chunk)

    # Allocate target tokens proportionally across chunks
    total_chars = sum(len(c) for c in final_chunks)
    summaries: list[str] = []

    for chunk in final_chunks:
        chunk_target = max(int(target_tokens * len(chunk) / max(total_chars, 1)), 100)
        summary = _llm_summarize_single(section.name, chunk, chunk_target, llm)
        if summary:
            summaries.append(summary)
        else:
            # Fallback: smart-truncate this chunk
            summaries.append(_smart_truncate(chunk, chunk_target))

    if not summaries:
        return None

    result = "\n\n".join(summaries)
    logger.info(
        "Chunked summarization for '%s': %d chars → %d chars (%d chunks)",
        section.name, len(content), len(result), len(final_chunks),
    )
    return result


def _smart_truncate(text: str, target_tokens: int) -> str:
    """Truncate text by keeping highest-value sections.

    Splits on markdown headings, scores by information density
    (shorter sections with numbers/formulas score higher),
    and reassembles in original order.
    """
    target_chars = target_tokens * 4  # approximate

    if len(text) <= target_chars:
        return text

    # Split by markdown headings
    parts = re.split(r"(^#{1,3}\s.+$)", text, flags=re.MULTILINE)

    # Recombine heading + body pairs
    segments: list[tuple[int, str, float]] = []  # (original_index, text, score)
    i = 0
    idx = 0
    while i < len(parts):
        if i + 1 < len(parts) and re.match(r"^#{1,3}\s", parts[i]):
            segment = parts[i] + parts[i + 1]
            i += 2
        else:
            segment = parts[i]
            i += 1

        if not segment.strip():
            idx += 1
            continue

        # Score: prefer segments with numbers, formulas, definitions
        score = 1.0
        numbers = len(re.findall(r"\d+\.?\d*", segment))
        score += min(numbers * 0.3, 3.0)  # numbers boost, capped
        if any(kw in segment.lower() for kw in ("formula", "definition", "=", "rule")):
            score += 2.0
        if idx == 0:
            score += 1.5  # first section (overview) bonus
        # Short, dense sections score higher
        if len(segment) < 500:
            score += 1.0

        segments.append((idx, segment, score))
        idx += 1

    if not segments:
        return text[:target_chars] + "\n... [truncated]"

    # Select highest-scoring segments until budget filled
    by_score = sorted(segments, key=lambda x: x[2], reverse=True)
    selected_indices: set[int] = set()
    used = 0
    for orig_idx, segment, _ in by_score:
        if used + len(segment) > target_chars:
            continue
        selected_indices.add(orig_idx)
        used += len(segment)

    # If nothing fits, take the highest-scored segment truncated to target
    if not selected_indices and by_score:
        best_idx, best_text, _ = by_score[0]
        truncated_best = best_text[:target_chars]
        return truncated_best + "\n... [truncated: kept first segment]"

    # Reassemble in original order
    result_parts = [seg for orig_idx, seg, _ in segments if orig_idx in selected_indices]
    result = "\n".join(result_parts)

    omitted = len(text) - len(result)
    if omitted > 0:
        result += f"\n\n... [truncated: kept {len(result):,}/{len(text):,} chars by relevance]"

    return result


def _hard_truncate_lowest(
    sections: list[Section],
    content_overrides: dict[int, str],
    total_tokens: int,
    budget: int,
) -> None:
    """Last resort: hard-truncate the lowest-priority compressible sections.

    Guarantees convergence: if the 500-token minimum per section still
    exceeds budget, subsequent sections are truncated more aggressively
    (down to 100 tokens minimum). On the final pass, sections can be
    truncated to near-zero.
    """
    compressible = sorted(
        [(i, s) for i, s in enumerate(sections) if s.compressible and s.content],
        key=lambda x: x[1].priority,
    )
    # Two passes: first with 500-token minimum, then 100 if still over
    for min_keep in (500, 100, 0):
        for idx, section in compressible:
            excess = total_tokens - budget
            if excess <= 0:
                return
            current_content = content_overrides.get(idx, section.content)
            section_tokens = estimate_tokens(current_content)
            keep_tokens = max(section_tokens - excess, min_keep)
            if keep_tokens >= section_tokens:
                continue  # Nothing to truncate
            keep_chars = max(keep_tokens * 4, 1)
            if keep_chars < len(current_content):
                truncated = (
                    current_content[:keep_chars]
                    + f"\n... [hard truncated: {keep_chars:,}/{len(current_content):,} chars]"
                )
                content_overrides[idx] = truncated
                total_tokens -= (section_tokens - estimate_tokens(truncated))


def _render(sections: list[Section]) -> str:
    """Render sections into a single prompt string, preserving original order."""
    parts: list[str] = []
    for s in sections:
        if not s.content or not s.content.strip():
            continue
        if s.heading:
            parts.append(f"{s.heading}\n{s.content}")
        else:
            parts.append(s.content)
    return "\n\n".join(parts)


def _render_with_overrides(sections: list[Section], overrides: dict[int, str]) -> str:
    """Render sections with content overrides, preserving original order."""
    parts: list[str] = []
    for i, s in enumerate(sections):
        content = overrides.get(i, s.content)
        if not content or not content.strip():
            continue
        if s.heading:
            parts.append(f"{s.heading}\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)
