"""SQLModel tables — aggregated here so that create_all registers them."""
from .audit import AuditLog
from .broadcast_schedule import BroadcastSchedule
from .cross_server import ChatBridgeMessage, CrossServerSettings
from .game_settings import GameSetting
from .incident import ServerIncident
from .install_setting import InstallSetting
from .job_run import JobRun
from .metric_sample import MetricSample
from .mod_set import ModChange, ModSet
from .operation import Operation
from .player import PlayerEvent, PlayerLink, PlayerSession
from .resource_budget import ResourceBudget
from .restart_schedule import RestartSchedule
from .role_permission import RolePermission
from .server import ConsoleKind, GameServer, GameType, ManagementMode
from .server_secret import ServerSecret
from .ticket import Ticket, TicketCategory, TicketComment, TicketStatus
from .user import PERMISSION_FLAGS, Role, User, UserStatus
from .version_catalog import VersionCatalogEntry
from .version_change import VersionChange

__all__ = [
    "User",
    "Role",
    "UserStatus",
    "PERMISSION_FLAGS",
    "RolePermission",
    "AuditLog",
    "Ticket",
    "TicketCategory",
    "TicketComment",
    "TicketStatus",
    "PlayerLink",
    "PlayerSession",
    "PlayerEvent",
    "RestartSchedule",
    "MetricSample",
    "ServerIncident",
    "JobRun",
    "BroadcastSchedule",
    # --- multi-game ---
    "GameServer",
    "GameType",
    "ManagementMode",
    "ConsoleKind",
    "ResourceBudget",
    "VersionChange",
    "VersionCatalogEntry",
    "ModSet",
    "ModChange",
    "Operation",
    "CrossServerSettings",
    "ChatBridgeMessage",
    "GameSetting",
    "ServerSecret",
    "InstallSetting",
]
