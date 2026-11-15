from __future__ import annotations

import pytest

from webskrap import Element, FetchResult, parse_html

PAGE = """<!doctype html>
<html><head><title>Shop</title></head><body>
<!-- note -->
<main id="products" data-kind="shop list">
  <article class="product featured" data-id="1" lang="en-GB">
    <h3>Product 1</h3><span class="price">$10.99</span><br><img src="a.png"/>
  </article>
  <article class="product" data-id="2" lang="en">
    <h3>Product &amp; 2</h3><span class="price">$20.99</span>
  </article>
</main>
<footer><h3>Contact</h3></footer>
</body></html>"""


def _tags(elements: list[Element]) -> list[str]:
    return [element.tag for element in elements]


def test_parse_builds_tree_with_decoded_text_and_attributes() -> None:
    root = parse_html('<P ID="x" ID="y" hidden>a &lt; b<!-- c --></P>')
    (paragraph,) = root.children
    assert isinstance(paragraph, Element)
    assert paragraph.tag == "p"
    assert paragraph.parent is root
    assert paragraph.attrs == {"id": "x", "hidden": ""}
    assert paragraph.text == "a < b"


def test_parse_handles_void_self_closing_stray_and_implied_end_tags() -> None:
    root = parse_html(
        "<ul><li>a<li>b</ul><p>one<p>two<div>three</div></span>"
        "<table><tr><td>1<td>2<tr><td>3</table><br><svg><path/><path/></svg>"
    )
    assert _tags(root.css("ul > li")) == ["li", "li"]
    assert [p.text for p in root.css("p")] == ["one", "two"]
    assert root.css_first("body") is None
    assert [cell.text for cell in root.css("tr > td")] == ["1", "2", "3"]
    assert len(root.css("table > tr")) == 2
    assert root.css("br *") == []
    assert len(root.css("svg > path")) == 2


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("h3", ["Product 1", "Product & 2", "Contact"]),
        ("main h3", ["Product 1", "Product & 2"]),
        ("#products > article > h3", ["Product 1", "Product & 2"]),
        ("body > h3", []),
        (".product.featured h3", ["Product 1"]),
        ("ARTICLE[data-id='2'] h3", ["Product & 2"]),
        ('[data-id="1"] h3', ["Product 1"]),
        ("[data-kind~=list] [data-id=1] h3", ["Product 1"]),
        ("[lang|=en] h3", ["Product 1", "Product & 2"]),
        ("[class^=prod][class$=featured] h3", ["Product 1"]),
        ("[class*=duct] h3", ["Product 1", "Product & 2"]),
        ("[class^=''] h3", []),
        ("footer h3 , main article > h3", ["Product 1", "Product & 2", "Contact"]),
        ("* > footer > *", ["Contact"]),
    ],
)
def test_css_selectors(selector: str, expected: list[str]) -> None:
    assert [element.text for element in parse_html(PAGE).css(selector)] == expected


@pytest.mark.parametrize(
    "selector", ["", "  ", "a >", "> a", "a,,b", "a,", "a + b", "a ~ b", "a:hover", "[x]div", "a]"]
)
def test_unsupported_or_malformed_selectors_raise(selector: str) -> None:
    with pytest.raises(ValueError):
        parse_html(PAGE).css(selector)


def test_deep_documents_do_not_recurse_or_go_quadratic() -> None:
    depth = 20_000
    root = parse_html("<div>" * depth + "x" + "</div>" * depth)
    assert root.text == "x"
    assert root.css("span div") == []
    assert len(root.css("div > div")) == depth - 1


def test_fetch_result_css_reparses_only_when_text_changes() -> None:
    result = FetchResult(
        url="https://example.com",
        final_url="https://example.com",
        status=200,
        ok=True,
        headers={},
        text=PAGE,
        title="Shop",
        cookies=[],
        timings={},
    )
    first = result.css_first("span.price")
    assert first is not None
    assert first.text == "$10.99"
    assert result.css_first("span.price") is first
    assert "_document" not in result.model_dump()
    result.text = "<span class=price>$1</span>"
    assert [element.text for element in result.css(".price")] == ["$1"]
    assert result.css_first("h3") is None
