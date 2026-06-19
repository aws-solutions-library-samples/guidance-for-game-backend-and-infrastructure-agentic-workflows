"""
Logger utility for the backend.

This module provides a configured logger for the backend with enhanced
MCP operation logging and error classification capabilities.
"""

# Standard library
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Third-party packages
from loguru import logger

# Local modules
from config import settings

# Detect if running in containerized environment (AgentCore Runtime)
IS_CONTAINERIZED = not settings.IS_DEVELOPMENT

# Configure logger with more detailed formatting
logger.remove()  # Remove default handler

# ALWAYS log to stdout (ADOT will capture it)
logger.add(
    sys.stdout,
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    backtrace=True,
    diagnose=True,
)

if IS_CONTAINERIZED:
    logger.info("Logger configured for containerized environment (stdout for ADOT)")
else:
    # Development: Log to stderr and files
    # Create logs directory if it doesn't exist
    log_dir = Path(settings.LOG_FILE).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    # Add stderr handler with detailed formatting
    logger.add(
        sys.stderr,
        level=settings.LOG_LEVEL,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )

    # Add file handler with even more detailed formatting for debugging
    logger.add(
        settings.LOG_FILE,
        rotation="5 MB",  # Rotate when file reaches 5MB
        retention="1 week",  # Keep logs for 1 week
        compression="zip",  # Compress rotated logs
        level="DEBUG",  # Always log DEBUG level to file for troubleshooting
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message} | {extra}",
        backtrace=True,
        diagnose=True,
    )

    # Add a separate error log file for easier error tracking
    error_log_file = os.path.join(os.path.dirname(settings.LOG_FILE), "backend_errors.log")
    logger.add(
        error_log_file,
        rotation="5 MB",
        retention="2 weeks",
        compression="zip",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}\n{exception}",
        backtrace=True,
        diagnose=True,
    )

    # Add MCP-specific log file for detailed MCP operations
    mcp_log_file = os.path.join(os.path.dirname(settings.LOG_FILE), "mcp_operations.log")
    logger.add(
        mcp_log_file,
        rotation="10 MB",
        retention="2 weeks",
        compression="zip",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | MCP | {extra[server_type]:<12} | {extra[operation]:<20} | {message}",
        filter=lambda record: record["extra"].get("mcp_operation", False),
        backtrace=True,
        diagnose=True,
    )

    logger.info(f"Logger initialized with level {settings.LOG_LEVEL}")
    logger.info(f"Logs will be written to {settings.LOG_FILE}")
    logger.info(f"Error logs will be written to {error_log_file}")
    logger.info(f"MCP operation logs will be written to {mcp_log_file}")


def log_mcp_operation(server_type: str, operation: str, message: str, level: str = "INFO", **extra_data):
    """
    Log MCP-specific operations with structured data

    Args:
        server_type: MCP server type (eks, aws_api, cost_explorer)
        operation: Operation being performed (start, stop, execute_tool, etc.)
        message: Log message
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        **extra_data: Additional structured data to include
    """
    log_data = {
        "mcp_operation": True,
        "server_type": server_type,
        "operation": operation,
        "timestamp": datetime.now().isoformat(),
        **extra_data,
    }

    # Use the appropriate log level
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(message, **log_data)


def log_mcp_error(server_type: str, operation: str, error: Exception, context: Optional[Dict[str, Any]] = None):
    """
    Log MCP errors with detailed context and classification

    Args:
        server_type: MCP server type
        operation: Operation that failed
        error: Exception that occurred
        context: Additional context information
    """
    error_data = {
        "mcp_operation": True,
        "server_type": server_type,
        "operation": operation,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "timestamp": datetime.now().isoformat(),
    }

    if context:
        error_data.update(context)

    logger.error(f"MCP {operation} failed for {server_type}: {str(error)}", **error_data)


def log_mcp_performance(server_type: str, operation: str, execution_time: float, success: bool, **metrics):
    """
    Log MCP performance metrics

    Args:
        server_type: MCP server type
        operation: Operation performed
        execution_time: Time taken in seconds
        success: Whether operation succeeded
        **metrics: Additional performance metrics
    """
    perf_data = {
        "mcp_operation": True,
        "server_type": server_type,
        "operation": operation,
        "execution_time": execution_time,
        "success": success,
        "timestamp": datetime.now().isoformat(),
        **metrics,
    }

    status = "✅" if success else "❌"
    logger.info(f"{status} MCP {operation} completed in {execution_time:.2f}s", **perf_data)


def log_mcp_fallback(server_type: str, tool_name: str, reason: str, fallback_type: str = "aws_sdk"):
    """
    Log MCP fallback usage with detailed reasoning

    Args:
        server_type: MCP server type
        tool_name: Tool that triggered fallback
        reason: Reason for fallback
        fallback_type: Type of fallback used
    """
    fallback_data = {
        "mcp_operation": True,
        "server_type": server_type,
        "operation": "fallback",
        "tool_name": tool_name,
        "reason": reason,
        "fallback_type": fallback_type,
        "timestamp": datetime.now().isoformat(),
    }

    logger.warning(
        f"🔄 MCP fallback triggered: {server_type}.{tool_name} -> {fallback_type} ({reason})", **fallback_data
    )


def log_mcp_health_check(server_type: str, status: str, metrics: Dict[str, Any]):
    """
    Log MCP health check results

    Args:
        server_type: MCP server type
        status: Health status (healthy, unhealthy, degraded)
        metrics: Health metrics
    """
    health_data = {
        "mcp_operation": True,
        "server_type": server_type,
        "operation": "health_check",
        "health_status": status,
        "timestamp": datetime.now().isoformat(),
        **metrics,
    }

    status_emoji = {"healthy": "✅", "unhealthy": "❌", "degraded": "⚠️"}.get(status, "❓")
    logger.info(f"{status_emoji} MCP health check: {server_type} is {status}", **health_data)


__all__ = [
    "logger",
    "log_mcp_operation",
    "log_mcp_error",
    "log_mcp_performance",
    "log_mcp_fallback",
    "log_mcp_health_check",
]


__all__ = [
    "logger",
    "log_mcp_operation",
    "log_mcp_error",
    "log_mcp_performance",
    "log_mcp_fallback",
    "log_mcp_health_check",
]
