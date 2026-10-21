"""User-facing settings, provider directory and credential storage."""

from codepilot.settings.models import (
    ROUTABLE_ROLES,
    AgentSettings,
    CredentialRef,
    ExecutionSettings,
    GeneralSettings,
    ModelConfig,
    ProviderConfig,
    RoleRoute,
    SettingsDocument,
    WorkspaceSettings,
)
from codepilot.settings.service import SettingsError, SettingsService

__all__ = [
    "ROUTABLE_ROLES",
    "AgentSettings",
    "CredentialRef",
    "ExecutionSettings",
    "GeneralSettings",
    "ModelConfig",
    "ProviderConfig",
    "RoleRoute",
    "SettingsDocument",
    "SettingsError",
    "SettingsService",
    "WorkspaceSettings",
]
