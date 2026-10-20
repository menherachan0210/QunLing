import codecs
import json

import pytest

from pkg.labelplus import TranslationError, output_name, parse_page, render_labelplus


def page(text="出发吧。", **overrides):
    label = dict(x=0.25, y=0.75, group=1, text=text)
    label.update(overrides)
    return {"status": "ok", "labels": [label]}


def test_labelplus_format_coordinates_and_multiline():
    raw = render_labelplus(
        [
            {"name": "001.png", "result": page("出发吧。\n我们走。")},
            {"name": "002.jpg", "result": page("咚！", group=2)},
        ]
    )
    assert raw.startswith(codecs.BOM_UTF8 + b"1,0\r\n-\r\n")
    text = raw.decode("utf-8-sig")
    assert (
        ">>>>>>>>[001.png]<<<<<<<<\r\n----------------[1]----------------[0.250,0.750,1]"
        in text
    )
    assert "出发吧。\r\n我们走。" in text
    assert text.count("----------------[1]----------------") == 2
    assert "\n" not in text.replace("\r\n", "")


@pytest.mark.parametrize(
    "override",
    [
        dict(x=-0.1),
        dict(y=1.1),
        dict(x=float("nan")),
        dict(x=True),
        dict(group=0),
        dict(group=True),
        dict(text=""),
        dict(text="\x00"),
        dict(text="  >>>>>>>>[evil.png]<<<<<<<<"),
        dict(text="----------------[2]----------------[0,0,1]"),
    ],
)
def test_invalid_geometry_and_parser_injection(override):
    with pytest.raises(TranslationError):
        parse_page(json.dumps(page(**override)))


def test_unreadable_is_failure_blank_is_empty_and_failed_page_visible():
    with pytest.raises(TranslationError):
        parse_page('{"status":"unreadable","labels":[]}')
    assert parse_page('{"status":"no_text","labels":[]}')["labels"] == []
    raw = render_labelplus(
        [
            {"name": "001.png", "result": None},
            {"name": "002.png", "result": {"status": "no_text", "labels": []}},
        ]
    )
    text = raw.decode("utf-8-sig")
    assert "本页未完成翻译" in text and ">>>>>>>>[002.png]<<<<<<<<" in text


def test_duplicate_pages_rejected_and_filename_sanitized():
    with pytest.raises(TranslationError):
        render_labelplus([{"name": "A.png"}, {"name": "a.PNG"}])
    name = output_name('../test:"bad.zip', True)
    assert name.endswith("_待校对_LabelPlus.txt")
    assert not any(c in name for c in '/\\:"')


def test_normal_brackets_preserved_but_page_terminator_rejected():
    raw = render_labelplus([{"name": "[组名]第01话.png", "result": page()}])
    assert ">>>>>>>>[[组名]第01话.png]<<<<<<<<" in raw.decode("utf-8-sig")
    with pytest.raises(TranslationError):
        render_labelplus([{"name": "name]<<<<<<<<.png", "result": page()}])


def test_full_hundred_page_budget_fits_the_file_transport_limit():
    result = parse_page(
        json.dumps(
            {
                "status": "ok",
                "labels": [
                    {"x": 0.5, "y": 0.5, "group": 1, "text": "中" * 75}
                    for _ in range(200)
                ],
            },
            ensure_ascii=False,
        )
    )
    raw = render_labelplus(
        [{"name": f"{i:03}.png", "result": result} for i in range(100)]
    )
    assert 1024 * 1024 < len(raw) < 8 * 1024 * 1024
