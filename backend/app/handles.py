"""Instagram handle normalization, shared by every place that stores or
looks up a `User.instagram_handle` (signup/profile, the feed, receipt
referrals) so they all agree on the same canonical form."""


def normalize_handle(raw: str) -> str:
    """Trim, drop a leading @, lowercase — matches the frontend's normalisation."""
    return (raw or "").strip().lstrip("@").lower()
