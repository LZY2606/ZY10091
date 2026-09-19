import html

import pytest

from marko import HTMLRenderer, Markdown
from marko.block import Document
from marko.renderer import Renderer


@pytest.mark.parametrize("reuse_renderer", [False, True])
@pytest.mark.parametrize("raise_inside", [False, True])
def test_nested_renderer_preserves_outer_context(reuse_renderer, raise_inside):
    outer = Renderer()
    inner = outer if reuse_renderer else Renderer()
    original = html._charref

    with outer:
        document = Document()
        outer.root_node = document
        assert html.unescape("&copy") == "&copy"

        try:
            with inner:
                if raise_inside:
                    raise ValueError("render failed")
        except ValueError:
            pass

        assert html.unescape("&copy") == "&copy"
        assert outer.root_node is document

    assert html._charref is original
    assert outer.root_node is None


def test_render_image_restores_dispatch_after_exception():
    """An exception while rendering image alt text must not poison the shared
    renderer instance for later calls (``self.render`` swap needs try/finally).
    """

    class RenderError(ValueError):
        pass

    class StrictHTMLRenderer(HTMLRenderer):
        def render_plain_text(self, element):
            text = element.children if isinstance(element.children, str) else ""
            if "boom" in text:
                raise RenderError("forbidden alt text")
            return super().render_plain_text(element)

    markdown = Markdown(renderer=StrictHTMLRenderer)

    with pytest.raises(RenderError, match="forbidden alt text"):
        markdown.convert("![boom](x.png)")

    assert markdown.renderer.render.__func__ is Renderer.render
    assert (
        markdown.convert("# heading\n\nnormal **bold** text")
        == "<h1>heading</h1>\n<p>normal <strong>bold</strong> text</p>\n"
    )
