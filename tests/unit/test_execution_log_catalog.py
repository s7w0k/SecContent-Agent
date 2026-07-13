"""L0 全链路日志事件字典契约测试。"""

import pytest
from execution_logs.catalog import (
    ACTION_SPECS,
    DETAIL_ALLOWLIST,
    SCOPE_POLICIES,
    SENSITIVE_KEY_NAMES,
    SENSITIVE_KEY_SUFFIXES,
    Action,
    ExecutionType,
    Phase,
    Relation,
    Scope,
    action_spec,
)


def test_all_enum_values_are_unique() -> None:
    for enum_type in (Scope, Relation, ExecutionType, Phase, Action):
        values = [item.value for item in enum_type]
        assert len(values) == len(set(values)), enum_type.__name__


def test_every_action_has_one_complete_spec_and_allowlist() -> None:
    assert set(ACTION_SPECS) == set(Action)
    assert set(DETAIL_ALLOWLIST) == set(Action)
    for action, spec in ACTION_SPECS.items():
        assert isinstance(spec.phase, Phase)
        assert spec.allowed_scopes
        assert spec.allowed_scopes <= set(Scope)
        assert DETAIL_ALLOWLIST[action] == spec.detail_fields


def test_public_and_private_actions_have_unambiguous_scope() -> None:
    assert action_spec(Action.SITE_FEED_RESULT).allowed_scopes == {Scope.SHARED}
    assert action_spec(Action.ARTICLES_UPSERTED).allowed_scopes == {Scope.SHARED}
    assert action_spec(Action.DRAFTS_GENERATED).allowed_scopes == {Scope.USER}
    assert action_spec(Action.CHAT_COMPLETED).allowed_scopes == {Scope.USER}
    assert action_spec(Action.FEEDBACK_RECORDED).allowed_scopes == {Scope.USER}


def test_scope_policy_freezes_multitenant_visibility() -> None:
    assert SCOPE_POLICIES[Scope.USER].owner_user_id_required is True
    assert SCOPE_POLICIES[Scope.SHARED].initiator_user_id_required is True
    assert SCOPE_POLICIES[Scope.SHARED].link_required_for_user_read is True
    assert SCOPE_POLICIES[Scope.SHARED].expose_initiator_user_id is False
    assert SCOPE_POLICIES[Scope.SHARED].expose_participant_user_ids is False
    assert SCOPE_POLICIES[Scope.SYSTEM].exposed_to_normal_user is False


def test_detail_allowlists_never_admit_secret_fields() -> None:
    for allowed_fields in DETAIL_ALLOWLIST.values():
        assert allowed_fields.isdisjoint(SENSITIVE_KEY_NAMES)
        assert not any(field.endswith(SENSITIVE_KEY_SUFFIXES) for field in allowed_fields)


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(ValueError):
        action_spec("invented_action")
