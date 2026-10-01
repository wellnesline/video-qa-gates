"""A14: source_urls 検証の単体テスト。

各 topic に source_urls が必須(最低 2 URL / https:// 形式)であることを検査する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from news_pipeline.qa import _check_source_urls


def _nc_with_topics(topics: list[dict]) -> dict:
    return {"intro": {"label_jp": "ニュース"}, "topics": topics}


def _topic(id_: str, source_urls) -> dict:
    return {
        "id": id_,
        "telop_cat": f"TOPIC {id_}",
        "telop_segs": [],
        "scenes": [
            {"template": "sparkline", "title": "S1", "value_main": "100"},
            {"template": "sparkline", "title": "S2", "value_main": "200"},
            {"template": "minimal", "title": "S3", "main_lines": []},
        ],
        "source_urls": source_urls,
    }


def test_a14_two_urls_pass():
    """2 URL あれば PASS。"""
    nc = _nc_with_topics([_topic("T1", ["https://a.example", "https://b.example"])])
    items = _check_source_urls(nc)
    assert any(i.code == "A14" and i.severity == "PASS" for i in items)


def test_a14_one_url_fail():
    """1 URL しかなければ FAIL。"""
    nc = _nc_with_topics([_topic("T1", ["https://a.example"])])
    items = _check_source_urls(nc)
    assert any(i.code == "A14" and i.severity == "FAIL" for i in items)


def test_a14_no_source_urls_key_fail():
    """source_urls キーが存在しない場合は FAIL。"""
    t = _topic("T1", [])
    t.pop("source_urls")
    nc = _nc_with_topics([t])
    items = _check_source_urls(nc)
    assert any(i.code == "A14" and i.severity == "FAIL" for i in items)


def test_a14_empty_list_fail():
    """空リストは FAIL。"""
    nc = _nc_with_topics([_topic("T1", [])])
    items = _check_source_urls(nc)
    assert any(i.code == "A14" and i.severity == "FAIL" for i in items)


def test_a14_non_https_url_fail():
    """https:// で始まらない URL が含まれる場合は FAIL。"""
    nc = _nc_with_topics([_topic("T1", ["http://a.example", "https://b.example"])])
    items = _check_source_urls(nc)
    assert any(i.code == "A14" and i.severity == "FAIL" for i in items)


def test_a14_multiple_topics_all_pass():
    """複数 topic すべて充足なら PASS。"""
    nc = _nc_with_topics([
        _topic("T1", ["https://a.example", "https://b.example"]),
        _topic("T2", ["https://c.example", "https://d.example"]),
    ])
    items = _check_source_urls(nc)
    assert any(i.code == "A14" and i.severity == "PASS" for i in items)


def test_a14_one_of_two_topics_fail():
    """1 topic でも不足があれば FAIL。"""
    nc = _nc_with_topics([
        _topic("T1", ["https://a.example", "https://b.example"]),
        _topic("T2", ["https://c.example"]),
    ])
    items = _check_source_urls(nc)
    assert any(i.code == "A14" and i.severity == "FAIL" for i in items)


def test_a14_three_urls_pass():
    """3 URL は 2 以上なので PASS。"""
    nc = _nc_with_topics([_topic("T1", ["https://a.example", "https://b.example", "https://c.example"])])
    items = _check_source_urls(nc)
    assert any(i.code == "A14" and i.severity == "PASS" for i in items)
