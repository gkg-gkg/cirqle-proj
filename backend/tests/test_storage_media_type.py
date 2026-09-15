"""media_type_for_key -- the extension->media-type lookup that lets Claude
verification declare the real image format instead of a hardcoded jpeg
guess (a mismatch makes Claude's base64 decode fail outright, independent
of the photo's actual clarity)."""
from app.storage import media_type_for_key


def test_jpg_extension():
    assert media_type_for_key("abc123.jpg") == "image/jpeg"


def test_jpeg_extension():
    assert media_type_for_key("abc123.jpeg") == "image/jpeg"


def test_png_extension():
    assert media_type_for_key("abc123.png") == "image/png"


def test_gif_extension():
    assert media_type_for_key("abc123.gif") == "image/gif"


def test_webp_extension():
    assert media_type_for_key("abc123.webp") == "image/webp"


def test_case_insensitive():
    assert media_type_for_key("ABC123.PNG") == "image/png"


def test_unknown_extension_falls_back_to_jpeg():
    assert media_type_for_key("abc123.heic") == "image/jpeg"


def test_empty_key_falls_back_to_jpeg():
    assert media_type_for_key("") == "image/jpeg"
    assert media_type_for_key(None) == "image/jpeg"
