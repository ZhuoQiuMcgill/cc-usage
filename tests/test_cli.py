"""CLI output-boundary behavior."""

from cc_usage.cli import _configure_unicode_output


class _Stream:
    def __init__(self, encoding="cp1252"):
        self.encoding = encoding
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)
        self.encoding = kwargs["encoding"]


def test_windows_output_is_reconfigured_to_utf8():
    stream = _Stream()
    _configure_unicode_output(stream, windows=True)
    assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_utf8_or_non_windows_output_is_untouched():
    utf8 = _Stream("utf-8")
    _configure_unicode_output(utf8, windows=True)
    assert utf8.calls == []

    cp1252 = _Stream()
    _configure_unicode_output(cp1252, windows=False)
    assert cp1252.calls == []
