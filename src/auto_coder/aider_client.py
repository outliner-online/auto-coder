"""
Optional, manually installed Aider Python API client for Auto-Coder.
"""

import contextlib
import io
import os
import time
from typing import Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .llm_output_logger import LLMOutputLogger
from .logger_config import get_logger
from .usage_marker_utils import has_usage_marker_match

logger = get_logger(__name__)


class AiderClient(LLMClientBase):
    """Aider Python API client for analyzing issues and generating solutions."""

    def __init__(
        self,
        backend_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
        openrouter_base_url: Optional[str] = None,
        use_noedit_options: bool = False,
    ) -> None:
        """Initialize the optional Aider Python API client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
                         If provided, will use config for this backend.
            api_key: API key for the backend (optional, for custom backends).
            base_url: Base URL for the backend (optional, for custom backends).
            openai_api_key: OpenAI API key (optional, for OpenAI-compatible backends).
            openai_base_url: O  penAI base URL (optional, for OpenAI-compatible backends).
            openrouter_api_key: OpenRouter API key (optional, for OpenRouter-compatible backends).
            openrouter_base_url: OpenRouter base URL (optional, for OpenRouter-compatible backends).
            use_noedit_options: If True, use options_for_noedit instead of options.
        """
        super().__init__()
        self.use_noedit_options = use_noedit_options
        config = get_llm_config()

        # If backend_name is provided, get config from that backend
        if backend_name:
            self.config_backend = config.get_backend_config(backend_name)
            # Use backend config model, fall back to default "aider"
            self.model_name = (self.config_backend and self.config_backend.model) or "aider"
            # Use options_for_noedit if use_noedit_options is True
            if use_noedit_options and self.config_backend and self.config_backend.options_for_noedit:
                self.options = self.config_backend.options_for_noedit
            else:
                self.options = (self.config_backend and self.config_backend.options) or []
            self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
            self.api_key = api_key or (self.config_backend and self.config_backend.api_key)
            self.base_url = base_url or (self.config_backend and self.config_backend.base_url)
            self.openai_api_key = openai_api_key or (self.config_backend and self.config_backend.openai_api_key)
            self.openai_base_url = openai_base_url or (self.config_backend and self.config_backend.openai_base_url)
            self.openrouter_api_key = openrouter_api_key or (self.config_backend and self.config_backend.openrouter_api_key)
            self.openrouter_base_url = openrouter_base_url or (self.config_backend and self.config_backend.openrouter_base_url)
            self.model_provider = self.config_backend and self.config_backend.model_provider
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []
        else:
            # Fall back to default aider config
            self.config_backend = config.get_backend_config("aider")
            self.model_name = (self.config_backend and self.config_backend.model) or "aider"
            # Use options_for_noedit if use_noedit_options is True
            if use_noedit_options and self.config_backend and self.config_backend.options_for_noedit:
                self.options = self.config_backend.options_for_noedit
            else:
                self.options = (self.config_backend and self.config_backend.options) or []
            self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
            self.api_key = api_key
            self.base_url = base_url
            self.openai_api_key = openai_api_key
            self.openai_base_url = openai_base_url
            self.openrouter_api_key = openrouter_api_key
            self.openrouter_base_url = openrouter_base_url
            self.model_provider = None
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []

        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.timeout = None

        # Validate required options for this backend
        if self.config_backend:
            required_errors = self.config_backend.validate_required_options()
            if required_errors:
                for error in required_errors:
                    logger.warning(error)

        # Initialize LLM output logger
        self.output_logger = LLMOutputLogger()

        # Load the manually installed API only for an explicitly selected Aider
        # client. An unrelated CLI invocation must never import this package.
        try:
            from aider.coders import Coder
            from aider.io import InputOutput
            from aider.models import Model
        except ImportError as exc:
            raise RuntimeError("Aider Python API is unavailable. Install aider-chat manually in the Python environment running Auto-Coder; Auto-Coder does not install it.") from exc
        self._coder_class = Coder
        self._io_class = InputOutput
        self._model_class = Model

    def switch_to_conflict_model(self) -> None:
        """No-op; aider has no model switching."""
        logger.info("AiderClient: switch_to_conflict_model noop")

    def switch_to_default_model(self) -> None:
        """No-op; aider has no model switching."""
        logger.info("AiderClient: switch_to_default_model noop")

    def _apply_options_to_env(self, options: list[str], env_vars: dict[str, str]) -> None:
        """Map CLI options to Aider environment variables."""
        i = 0
        while i < len(options):
            opt = options[i]
            if not opt.startswith("--"):
                i += 1
                continue

            key = opt.lstrip("-").replace("-", "_").upper()
            env_key = f"AIDER_{key}"

            # Check if this option is a flag or takes a value
            # Heuristic: if next element exists and doesn't start with -, it's a value.
            # Exception: --no-foo is always a boolean flag (false).

            if opt.startswith("--no-"):
                # Handle --no-foo -> AIDER_FOO=false
                # key is NO_FOO. We want FOO.
                real_key = key.replace("NO_", "", 1)
                env_vars[f"AIDER_{real_key}"] = "false"
                i += 1
            else:
                # Check for value
                if i + 1 < len(options) and not options[i + 1].startswith("-"):
                    val = options[i + 1]
                    env_vars[env_key] = val
                    i += 2
                else:
                    # Boolean flag
                    env_vars[env_key] = "true"
                    i += 1

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run the optional Aider Python API and return only its model response."""
        is_noedit = is_noedit or self.use_noedit_options
        start_time = time.time()
        status = "success"
        error_message = None
        full_output = ""

        try:
            # Prepare environment variables
            env_vars = {}
            if self.api_key:
                env_vars["AIDER_API_KEY"] = self.api_key
            if self.base_url:
                env_vars["AIDER_BASE_URL"] = self.base_url
            if self.openai_api_key:
                env_vars["OPENAI_API_KEY"] = self.openai_api_key
            if self.openai_base_url:
                env_vars["OPENAI_BASE_URL"] = self.openai_base_url
            if self.openrouter_api_key:
                env_vars["OPENROUTER_API_KEY"] = self.openrouter_api_key
            if self.openrouter_base_url:
                env_vars["OPENROUTER_BASE_URL"] = self.openrouter_base_url

            # Set aider env vars for non-interactive mode
            env_vars["AIDER_GUI"] = "false"
            env_vars["AIDER_BROWSER"] = "false"
            env_vars["AIDER_CHECK_UPDATE"] = "false"
            env_vars["AIDER_ANALYTICS"] = "false"
            env_vars["AIDER_NO_STREAM"] = "true"

            # Apply usage markers (handled by capturing output)
            if self.usage_markers and isinstance(self.usage_markers, (list, tuple)):
                usage_markers = self.usage_markers
            else:
                usage_markers = [
                    "rate limit",
                    "usage limit",
                    "upgrade to pro",
                    "too many requests",
                ]

            if self.config_backend:
                processed_options = self.config_backend.replace_placeholders(model_name=self.model_name, session_id=None)
                options = processed_options["options_for_noedit"] if is_noedit and self.options_for_noedit else processed_options["options"]
            else:
                options = self.options_for_noedit if is_noedit and self.options_for_noedit else self.options

            self._apply_options_to_env(options, env_vars)
            self._apply_options_to_env(self.consume_extra_args(), env_vars)
            original_env = os.environ.copy()
            diagnostics = io.StringIO()
            try:
                os.environ.update(env_vars)
                # Aider's existing Python scripting API returns model content
                # directly. Its terminal output is a separate diagnostic channel.
                with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
                    io_obj = self._io_class(yes=True)
                    model = self._model_class(self.model_name)
                    coder = self._coder_class.create(
                        main_model=model,
                        fnames=[],
                        io=io_obj,
                        auto_commits=False,
                        dirty_commits=False,
                        dry_run=is_noedit,
                        edit_format="ask" if is_noedit else None,
                    )
                    run_result = coder.run(prompt)
                full_output = run_result.strip() if isinstance(run_result, str) else ""
            finally:
                os.environ.clear()
                os.environ.update(original_env)

            # Diagnostics can signal a usage limit but must never be presented
            # as a model answer, including when the API returns no content.
            if has_usage_marker_match(full_output, usage_markers) or has_usage_marker_match(diagnostics.getvalue(), usage_markers):
                raise AutoCoderUsageLimitError("Aider reported a usage limit")
            if not full_output:
                raise RuntimeError("Aider returned no model response")

            return full_output

        except Exception as e:
            status = "error"
            error_message = str(e)
            raise
        finally:
            # Always log the interaction and print summary
            duration_ms = (time.time() - start_time) * 1000

            # Log to JSON file
            self.output_logger.log_interaction(
                backend="aider",
                model=self.model_name,
                prompt=prompt,
                response=full_output,
                duration_ms=duration_ms,
                status=status,
                error=error_message,
            )

            # Print user-friendly summary to stdout
            print("\n" + "=" * 60)
            print("🤖 Aider API Execution Summary")
            print("=" * 60)
            print(f"Backend: aider")
            print(f"Model: {self.model_name}")
            print(f"Prompt Length: {len(prompt)} characters")
            print(f"Response Length: {len(full_output)} characters")
            print(f"Duration: {duration_ms:.0f}ms")
            print(f"Status: {status.upper()}")
            if error_message:
                print("Error: Aider execution failed; see the interaction log.")
            print("=" * 60 + "\n")

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Aider CLI.

        Aider doesn't seem to have native MCP support in the same way Codex might,
        or at least not documented in the help output I saw.
        For now, we'll return False or implement if Aider adds support.
        """
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Aider CLI config.

        Not currently supported for Aider.
        """
        logger.warning("MCP server configuration not supported for Aider backend yet.")
        return False
