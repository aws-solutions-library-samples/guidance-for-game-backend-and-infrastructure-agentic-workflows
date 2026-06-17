"""
Agent execution timing wrapper to break down LLM vs MCP performance.
"""

# Standard library
import time
from typing import Any

# Local modules
from utils.logger import logger
from utils.timing import time_operation


class TimedAgent:
    """Wrapper around Strands Agent to add granular timing."""

    def __init__(self, agent):
        self.agent = agent
        self.execution_start = None

    def __call__(self, query: str) -> Any:
        """Execute agent with detailed timing breakdown."""

        with time_operation("timed_agent_total"):
            logger.info("🔍 DETAILED AGENT EXECUTION TRACE START")

            try:
                self.execution_start = time.time()
                logger.info("🧠 Starting agent execution (LLM + MCP processing)...")

                result = self.agent(query)

                total_time = time.time() - self.execution_start

                logger.info("🔍 DETAILED TIMING BREAKDOWN:")
                logger.info(f"  📊 Total agent execution: {total_time:.3f}s")
                logger.info(f"  🧠 This includes: LLM processing + MCP tool calls + framework overhead")
                logger.info(f"  🔧 Note: Individual LLM/MCP breakdown requires deeper instrumentation")

                return result

            except Exception as e:
                if self.execution_start:
                    failed_time = time.time() - self.execution_start
                    logger.error(f"❌ Agent execution failed after {failed_time:.3f}s: {e}")
                else:
                    logger.error(f"❌ Agent execution failed immediately: {e}")
                raise
