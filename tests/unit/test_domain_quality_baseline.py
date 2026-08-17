from agent.harness.domain_quality_baseline import (
    QualityScores,
    ReviewerAnnotation,
    calculate_agreement,
)


def _scores(**overrides):
    base = {
        "classification_correct": True,
        "product_relevance": 3,
        "score_in_expert_range": True,
        "factual_completeness": 4,
        "citation_quality": 3,
        "structure_quality": 3,
        "product_accuracy": 4,
        "style_quality": 3,
        "promotion_risk": 0,
        "revision_instruction_following": 4,
        "non_target_preservation": 4,
    }
    base.update(overrides)
    return QualityScores(**base)


def test_agreement_requires_paired_reviewers_and_is_deterministic():
    annotations = [
        ReviewerAnnotation(sample_id="s1", reviewer_id="r1", scores=_scores()),
        ReviewerAnnotation(
            sample_id="s1",
            reviewer_id="r2",
            scores=_scores(product_relevance=2, style_quality=2),
        ),
        ReviewerAnnotation(sample_id="s2", reviewer_id="r1", scores=_scores()),
    ]
    result = calculate_agreement(annotations)
    assert result["paired_samples"] == 1
    assert result["categorical_agreement"] == 1.0
    assert result["numeric_mae"] == 0.2222
