"""Small helpers for safe Cursor-on-Target XML construction."""

from xml.sax.saxutils import escape, quoteattr


def xml_text(value):
    """Escape a value for use as XML element text."""
    return escape(str(value))


def xml_attr(value):
    """Escape and quote a value for use as an XML attribute."""
    return quoteattr(str(value))
