class AriaError(RuntimeError):
    """Base error that can be safely shown by the CLI."""


class ConfigurationError(AriaError):
    """Configuration is missing or violates a boundary."""


class AccessPolicyError(AriaError):
    """A requested document path violates the access policy."""


class WorkflowError(AriaError):
    """A workflow definition or transition is invalid."""


class ProviderAdapterError(AriaError):
    """A provider adapter could not produce authenticated read-back."""


class ProviderCapabilityError(ProviderAdapterError):
    """A required provider capability is unavailable or cannot be verified."""


class PreflightError(AriaError):
    """Doctor found a blocking condition."""
