"""One place that decides whether a password is acceptable.

Applied identically at signup, at reset, and at change — inconsistent rules
between those three is a classic way a weak password sneaks in through the
back door.
"""
from fastapi import HTTPException

MIN_LENGTH = 10

# The handful that dominate every breach dump. Not a substitute for a real
# breach-corpus check, but it costs nothing and stops the worst offenders.
COMMON = {
    "password", "password1", "password123", "passw0rd", "12345678", "123456789",
    "1234567890", "qwertyuiop", "qwerty123", "letmein123", "welcome123",
    "admin123", "iloveyou", "sunshine", "princess", "football", "baseball",
    "monkey123", "dragon123", "trustno1", "changeme", "secret123", "abc12345",
    "cirqle123", "cashback",
}


def validate(password: str, email: str = "") -> None:
    """Raise 422 with a message the user can act on, or return quietly."""
    if len(password) < MIN_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at least {MIN_LENGTH} characters.")
    lowered = password.lower()
    if lowered in COMMON:
        raise HTTPException(
            status_code=422,
            detail="That password is too common. Please choose a different one.")
    # The email is public knowledge, so a password built from it is guessable.
    if email:
        local = email.split("@")[0].lower()
        if local and (lowered == local or lowered == email.lower()):
            raise HTTPException(
                status_code=422,
                detail="Your password can't be your email address.")
