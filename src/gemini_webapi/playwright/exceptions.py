class CaptchaError(Exception):
    """Raised when Google CAPTCHA or unusual traffic page is detected."""
    pass


class LogoutError(Exception):
    """Raised when Google account is logged out or session expired."""
    pass
