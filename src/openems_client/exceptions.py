"""Exceptions raised by the OpenEMS client."""


class OpenEMSError(Exception):
    """Base error of the OpenEMS client, also raised when the backend rejects a request."""


class OpenEMSConnectionError(OpenEMSError):
    """The backend cannot be reached or does not respond in time."""


class OpenEMSAuthError(OpenEMSError):
    """The backend rejects the login."""
