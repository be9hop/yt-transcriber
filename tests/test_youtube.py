"""URL validation matrix: accepts only strict YouTube video URLs."""

import pytest

from app.errors import AppError
from app.youtube import extract_video_id

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # watch URLs
        (f"https://www.youtube.com/watch?v={VID}", VID),
        (f"https://youtube.com/watch?v={VID}", VID),
        (f"http://www.youtube.com/watch?v={VID}", VID),
        (f"https://www.youtube.com/watch?v={VID}&t=30s&list=PL1234567890123456789012", VID),
        # alternate hosts
        (f"https://m.youtube.com/watch?v={VID}", VID),
        (f"https://music.youtube.com/watch?v={VID}", VID),
        (f"https://www.youtube-nocookie.com/watch?v={VID}", VID),
        (f"https://www.youtube-nocookie.com/embed/{VID}", VID),
        # path-embedded IDs
        (f"https://youtu.be/{VID}", VID),
        (f"https://youtu.be/{VID}?t=42", VID),
        (f"https://youtu.be/{VID}/", VID),  # bare trailing slash tolerated
        (f"https://www.youtube.com/shorts/{VID}/", VID),
        (f"https://www.youtube.com/shorts/{VID}", VID),
        (f"https://www.youtube.com/embed/{VID}", VID),
        (f"https://www.youtube.com/live/{VID}", VID),
        (f"https://m.youtube.com/shorts/{VID}", VID),
        # uppercase host (and scheme) must be normalized
        (f"HTTPS://WWW.YOUTUBE.COM/watch?v={VID}", VID),
        (f"https://YOUTU.BE/{VID}", VID),
    ],
)
def test_accepts(url, expected):
    assert extract_video_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # non-youtube hosts and lookalikes
        f"https://example.com/watch?v={VID}",
        f"https://youtube.com.evil.com/watch?v={VID}",
        f"https://youtu.be.evil.com/{VID}",
        "https://evil.com/youtu.be/dQw4w9WgXcQ",
        # userinfo tricks
        f"https://youtube.com@evil.com/watch?v={VID}",
        # schemes
        f"javascript:alert('//youtu.be/{VID}')",
        f"ftp://youtu.be/{VID}",
        f"data:text/html,https://youtu.be/{VID}",
        VID,  # bare id, no scheme/host
        "not a url",
        "",
        "   ",
        None,
        # missing / garbage IDs
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?vi=abcdefghijk",
        "https://www.youtube.com/watch?v=tooshort",
        f"https://www.youtube.com/watch?v={VID}EXTRA",  # 13 chars
        f"https://www.youtube.com/watch?v={VID.replace('Q', '?')}",  # bad char
        "https://youtu.be/",
        f"https://www.youtube.com/shorts/{VID}/extra/more",
        # path traversal attempts
        f"https://www.youtube.com/shorts/../../etc/passwd",
        f"https://www.youtube.com/watch?v=../../etc/passwd",
        f"https://youtu.be/%2e%2e%2f%2e%2e%2fetc",
        # playlists are not videos
        "https://www.youtube.com/playlist?list=PL1234567890123456789012",
    ],
)
def test_rejects(url):
    with pytest.raises(AppError) as excinfo:
        extract_video_id(url)
    assert excinfo.value.code == "invalid_url"
    assert excinfo.value.http_status == 400


def test_extracted_id_is_the_only_thing_passed_on():
    # The guard's contract: output is exactly the 11-char ID.
    assert len(extract_video_id(f"https://www.youtube.com/watch?v={VID}&t=1")) == 11
