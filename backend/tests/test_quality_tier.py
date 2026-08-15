import pytest

from pensieve.services import quality
from tests.conftest import make_settings


def test_1080_is_auto_4k_needs_approval():
    assert quality.decide_tier("1080p") == "auto"
    assert quality.decide_tier("4K") == "approval"
    with pytest.raises(ValueError):
        quality.decide_tier("8K")


def test_plan_action_switches_low_profile_up_to_hd(tmp_path):
    s = make_settings(tmp_path)  # radarr hd=6, 4k=7
    plan = quality.plan_action(media_type="movie", requested="1080p",
                               current_profile_id=3, settings=s)
    assert plan == {"tier": "auto", "target_profile_id": 6,
                    "needs_profile_switch": True}


def test_plan_action_no_switch_when_already_on_target(tmp_path):
    s = make_settings(tmp_path)
    plan = quality.plan_action(media_type="series", requested="1080p",
                               current_profile_id=4, settings=s)
    assert plan["needs_profile_switch"] is False


def test_plan_action_4k_requires_approval(tmp_path):
    s = make_settings(tmp_path)
    plan = quality.plan_action(media_type="movie", requested="4K",
                               current_profile_id=6, settings=s)
    assert plan == {"tier": "approval", "target_profile_id": 7,
                    "needs_profile_switch": True}
