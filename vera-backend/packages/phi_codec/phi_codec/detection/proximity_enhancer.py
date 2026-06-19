"""Proximity-weighted context enhancer.

Presidio's stock ``LemmaContextAwareEnhancer`` adds a flat boost if *any* of a
recognizer's context words appears in the surrounding window — distance-blind. In a
long payer-call sentence that lets far-away cues bleed across and outscore the local
one, e.g. "member/group" (15 tokens back) beating "reach … at" (3 tokens back), so a
contact number types as MEMBER_ID instead of PHONE.

This subclass scales the boost by token distance to the *nearest* matching context
word: full boost within ``near_tokens``, linear decay to zero by ``window_tokens``.
The min-score floor is only applied for a strong (near) match, so a far/weak cue
nudges the score without forcing the floor. Everything else (recognizer lookup,
already-boosted skip, substring matching) mirrors the parent.
"""

from __future__ import annotations

import copy
from typing import List, Optional

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.context_aware_enhancers import (
    ContextAwareEnhancer,
    LemmaContextAwareEnhancer,
)
from presidio_analyzer.nlp_engine import NlpArtifacts


class ProximityContextEnhancer(LemmaContextAwareEnhancer):
    def __init__(
        self,
        context_similarity_factor: float = 0.35,
        min_score_with_context_similarity: float = 0.4,
        near_tokens: int = 3,
        window_tokens: int = 12,
    ) -> None:
        # prefix/suffix counts are unused here (we scan both directions ourselves),
        # but the parent constructor requires them.
        super().__init__(
            context_similarity_factor=context_similarity_factor,
            min_score_with_context_similarity=min_score_with_context_similarity,
            context_prefix_count=window_tokens,
            context_suffix_count=window_tokens,
        )
        self.near_tokens = near_tokens
        self.window_tokens = window_tokens

    def _distance_weight(self, dist: int) -> float:
        """1.0 within near_tokens, linear decay to 0.0 at window_tokens."""
        if dist <= self.near_tokens:
            return 1.0
        if dist >= self.window_tokens:
            return 0.0
        return (self.window_tokens - dist) / (self.window_tokens - self.near_tokens)

    def enhance_using_context(
        self,
        text: str,
        raw_results: List[RecognizerResult],
        nlp_artifacts: NlpArtifacts,
        recognizers: List[EntityRecognizer],
        context: Optional[List[str]] = None,
    ) -> List[RecognizerResult]:
        results = copy.deepcopy(raw_results)
        recognizers_dict = {r.id: r for r in recognizers}
        extra_context = [w.lower() for w in context] if context else []

        if nlp_artifacts is None or not nlp_artifacts.tokens:
            return results

        lemmas = nlp_artifacts.lemmas

        for result in results:
            recognizer = self._recognizer_for(result, recognizers_dict)
            if not recognizer or not recognizer.context:
                continue
            if result.recognition_metadata.get(
                RecognizerResult.IS_SCORE_ENHANCED_BY_CONTEXT_KEY
            ):
                continue

            word = text[result.start : result.end]
            try:
                token_index = self._find_index_of_match_token(
                    word, result.start, nlp_artifacts.tokens, nlp_artifacts.tokens_indices
                )
            except ValueError:
                continue

            best_weight, best_word = self._nearest_context(
                token_index, lemmas, recognizer.context
            )
            # Sentence-level extra context (passed in by caller) has no position;
            # treat it as a weak, far match so it can't dominate a local cue.
            if not best_weight and extra_context:
                far = self._find_supportive_word_in_context(
                    extra_context, recognizer.context, self.context_matching_mode
                )
                if far:
                    best_weight, best_word = self._distance_weight(self.window_tokens - 1), far

            if best_weight <= 0:
                continue

            strong = best_weight >= 1.0
            result.score += self.context_similarity_factor * best_weight
            if strong:
                result.score = max(result.score, self.min_score_with_context_similarity)
            result.score = min(result.score, ContextAwareEnhancer.MAX_SCORE)
            result.analysis_explanation.set_supportive_context_word(best_word)
            result.analysis_explanation.set_improved_score(result.score)

        return results

    @staticmethod
    def _recognizer_for(result, recognizers_dict):
        meta = result.recognition_metadata or {}
        rec_id = meta.get(RecognizerResult.RECOGNIZER_IDENTIFIER_KEY)
        return recognizers_dict.get(rec_id) if rec_id else None

    def _nearest_context(self, token_index, lemmas, ctx) -> tuple[float, str]:
        """Best (distance-weight, matched word) over context words near the match."""
        best_weight = 0.0
        best_word = ""
        n = len(lemmas)
        for dist in range(1, self.window_tokens + 1):
            for idx in (token_index - dist, token_index + dist):
                if 0 <= idx < n:
                    matched = self._find_supportive_word_in_context(
                        [lemmas[idx]], ctx, self.context_matching_mode
                    )
                    if matched:
                        w = self._distance_weight(dist)
                        if w > best_weight:
                            best_weight, best_word = w, matched
            if best_weight >= 1.0:  # can't beat a near match; stop early
                break
        return best_weight, best_word
