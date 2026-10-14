"""Python implementation of the Cordis plugin runtime."""

from .config import ConfigValidator, ValidationError
from .context import Context
from .effect import Disposable, Effect, EffectMeta
from .errors import CordisError, CordisErrorCode
from .events import DispatchMode, EventOptions, EventsService, Hook, isBailed
from .fiber import Fiber, FiberState, resolveConfig
from .logger import (
    Exporter,
    Formatter,
    Logger,
    LoggerLevel,
    LoggerMethod,
    LoggerOptions,
    LoggerService,
    LoggerType,
    Message,
    c16,
    c256,
    defaultFormatters,
)
from .model import Inject, Plugin
from .reflect import Impl, Property, ReflectService
from .registry import PluginRuntime, RegistryService
from .service import Service
from .typing import PluginContext, PluginFunction, PluginObject
from .utils import DisposableList

__version__ = "0.1.0"

__all__ = [
    "ConfigValidator",
    "Context",
    "CordisError",
    "CordisErrorCode",
    "DispatchMode",
    "Disposable",
    "DisposableList",
    "Effect",
    "EffectMeta",
    "EventOptions",
    "EventsService",
    "Exporter",
    "Fiber",
    "FiberState",
    "Formatter",
    "Hook",
    "Impl",
    "Inject",
    "Logger",
    "LoggerLevel",
    "LoggerMethod",
    "LoggerOptions",
    "LoggerService",
    "LoggerType",
    "Message",
    "Plugin",
    "PluginContext",
    "PluginFunction",
    "PluginObject",
    "PluginRuntime",
    "Property",
    "ReflectService",
    "RegistryService",
    "Service",
    "ValidationError",
    "__version__",
    "c16",
    "c256",
    "defaultFormatters",
    "isBailed",
    "resolveConfig",
]
