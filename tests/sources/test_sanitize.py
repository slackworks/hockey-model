"""Tests for the market-field sanitizer."""

import json

import pytest

from hockey_model.sources.sanitize import MARKET_KEYS, strip_market_fields


def test_it_removes_a_market_key_at_the_top() -> None:
    assert strip_market_fields({"pickcenter": [1], "events": []}) == {"events": []}


@pytest.mark.parametrize("key", MARKET_KEYS)
def test_it_removes_every_market_key(key: str) -> None:
    assert strip_market_fields({key: {"x": 1}, "keep": 2}) == {"keep": 2}


def test_it_removes_a_market_key_at_two_depths() -> None:
    payload = {
        "competitions": [
            {"odds": {"spread": 1}, "competitors": [{"againstTheSpread": 2, "team": "BOS"}]}
        ],
        "pickcenter": {"providers": []},
    }
    stripped = strip_market_fields(payload)
    assert json.dumps(stripped, sort_keys=True) == json.dumps(
        {"competitions": [{"competitors": [{"team": "BOS"}]}]}, sort_keys=True
    )


def test_it_keeps_a_key_that_holds_a_market_word() -> None:
    """A key that only holds the word is not a market key."""
    payload = {"oddsProviderCount": 3, "againstTheSpreadNote": "x"}
    assert strip_market_fields(payload) == payload


def test_it_does_not_change_the_input() -> None:
    payload = {"odds": [1, 2], "keep": {"pickcenter": True}}
    strip_market_fields(payload)
    assert payload == {"odds": [1, 2], "keep": {"pickcenter": True}}


@pytest.mark.parametrize("payload", [None, 3, 1.5, True, "text"])
def test_a_plain_value_comes_back_as_it_is(payload: object) -> None:
    assert strip_market_fields(payload) is payload


def test_it_keeps_the_list_shape_when_a_market_key_was_the_only_key() -> None:
    assert strip_market_fields([{"odds": 1}, {"keep": 2}]) == [{}, {"keep": 2}]


def test_it_removes_a_market_key_deep_in_a_nested_list() -> None:
    payload = {"a": [{"b": [{"c": {"odds": 1, "keep": 2}}]}]}
    assert strip_market_fields(payload) == {"a": [{"b": [{"c": {"keep": 2}}]}]}
