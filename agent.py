# agent.py - Modular agent core
from __future__ import annotations
import json
import os
import queue
import pprint
from datetime import datetime
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from pydantic import ValidationError
from llm_providers.factory import ProviderFactory
from llm_providers.exceptions import ProviderError, RateLimitExceeded
from tools import SIMPLIFIED_TOOL_CLASSES
from tools.utils import model_to_openai_tool
from tools.final import Final
from tools.request_user_interaction import RequestUserInteraction
from tools.summarize_tool import SummarizeTool
from fast_json_repair import loads as repair_loads
from session.models import RuntimeParams
import tiktoken
import traceback
# Import security module for capability checks
try:
    from thoughtmachine.security import CapabilityRegistry, set_logger as security_set_logger
    SECURITY_AVAILABLE = True
except ImportError:
    SECURITY_AVAILABLE = False

# Import logging module
try:
    from agent_logging import create_logger
    LOGGING_AVAILABLE = True
except ImportError:
    LOGGING_AVAILABLE = False
    create_logger = None

if TYPE_CHECKING:
    from agent_core import AgentConfig
# prune_conversation_history imported inside process_query method
from agent_state import AgentState, TokenState, TurnState, ExecutionState, SessionState

class Agent:
    # Constants for context window management
    SAFETY_MARGIN = 1000  # tokens reserved for safety
    DEFAULT_RESPONSE_TOKENS = 4096  # default response tokens if max_tokens not set

    def __init__(self, config: AgentConfig, session=None, initial_conversation=None, session_id: str = None):
        self.config = config
        self.logger = None
        self.session = session
        if session is not None:
            # Use session's user_history as the conversation source (no copy)
            self.session_id = session.session_id
            self.conversation = session.user_history
            
            # DEBUG: Print original session history
            print("\n=== AGENT INIT WITH SESSION ===")
            print(f"Session ID: {self.session_id}")
            print(f"Original session.user_history length: {len(session.user_history)}")
            print("--- Original history ---")
            for i, msg in enumerate(session.user_history):
                role = msg.get('role', 'NO_ROLE')
                content_preview = str(msg.get('content', ''))[:80].replace('\n', ' ')
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', 'NO_ID')
                    print(f"  [{i}] {role}: tool_call_id={tool_call_id}, content={content_preview}...")
                elif role == 'assistant':
                    tool_calls = msg.get('tool_calls')
                    if tool_calls:
                        print(f"  [{i}] {role}: HAS TOOL_CALLS {len(tool_calls)}, content={content_preview}...")
                    else:
                        print(f"  [{i}] {role}: {content_preview}...")
                else:
                    print(f"  [{i}] {role}: {content_preview}...")
            
            # Reconstruct pruned conversation: sysprompt + most recent summary + recent turns
            self._reconstruct_pruned_conversation_from_session()
            
            # DEBUG: Print reconstructed conversation
            print("\n--- Reconstructed conversation ---")
            print(f"Reconstructed conversation length: {len(self.conversation)}")
            for i, msg in enumerate(self.conversation):
                role = msg.get('role', 'NO_ROLE')
                content_preview = str(msg.get('content', ''))[:80].replace('\n', ' ')
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', 'NO_ID')
                    print(f"  [{i}] {role}: tool_call_id={tool_call_id}, content={content_preview}...")
                elif role == 'assistant':
                    tool_calls = msg.get('tool_calls')
                    if tool_calls:
                        print(f"  [{i}] {role}: HAS TOOL_CALLS {len(tool_calls)}, content={content_preview}...")
                    else:
                        print(f"  [{i}] {role}: {content_preview}...")
                else:
                    print(f"  [{i}] {role}: {content_preview}...")
            print("=== END DEBUG ===\n")
        else:
            self.session = None
            self.session_id = session_id
            self.conversation = initial_conversation.copy() if initial_conversation else []

        # Create LLM provider using factory
        provider_config = {
            "api_key": config.api_key,
            "base_url": config.base_url,
            "model": config.model,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            **config.provider_config  # Merge any provider-specific config
        }
        self.provider = ProviderFactory.create_provider(
            config.provider_type,
            **provider_config
        )
        # Initialize mutable runtime parameters from config
        self.runtime_params = RuntimeParams(
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            top_p=None  # not in config
        )
        self.logger = None
        if LOGGING_AVAILABLE and config.enable_logging:

        # Initialize security module with logger if available
            if SECURITY_AVAILABLE:
                security_set_logger(self.logger)
                self.logger = create_logger(config)
        # Prepare tool definitions
        self.tool_classes = config.tool_classes if config.tool_classes is not None else SIMPLIFIED_TOOL_CLASSES
        self.tool_definitions = [model_to_openai_tool(cls) for cls in self.tool_classes]
        
        # Initialize token encoder
        self._token_encoder = None

        # Ensure system prompt is present (only if conversation is empty or no system)
        self._ensure_system_prompt()
        # Initialize context builder for LLM context generation
        self.context_builder = self._create_context_builder()

        # Token totals
        if session is not None:
            self.total_input_tokens = session.total_input_tokens
            self.total_output_tokens = session.total_output_tokens
        else:
            self.total_input_tokens = config.initial_input_tokens
            self.total_output_tokens = config.initial_output_tokens

        # Stop check
        self.stop_check = config.stop_check
        # Keep-alive queue and flags
        self._next_query_queue = queue.Queue()
        self._paused = False
        self._should_reset = False
        # Rate limiting state
        self.rate_limit_delay = 1.0  # seconds between turns when rate limited
        self.rate_limit_base_wait = 10.0  # initial wait when rate limit hit
        self.rate_limit_backoff_factor = 1.2  # multiply delay by this factor on repeated errors
        self.rate_limit_count = 0  # number of consecutive rate limit errors
        self.rate_limit_max_wait = 60.0  # maximum wait between turns
        self.rate_limit_active = False  # whether we're currently in rate limited mode
        # State management
        self.state = AgentState(self.config, self.logger)

        # Set session state based on whether we have existing history
        if self.session is not None:
            if len(self.session.user_history) > 0:
                events = self.state.set_session_state(SessionState.CONTINUING)
                for event in events:
                    self._handle_state_event(event)
            # else: new session with no history, keep default NEW state
        else:
            if initial_conversation is not None and len(initial_conversation) > 0:
                events = self.state.set_session_state(SessionState.CONTINUING)
                for event in events:
                    self._handle_state_event(event)
            # else: new session

        # Calculate initial token count for the conversation
        self._update_conversation_token_estimate()        
    def reset_rate_limiting(self):
        """Reset rate limiting state when user restarts the agent."""
        self.rate_limit_delay = 1.0
        self.rate_limit_count = 0
        self.rate_limit_active = False
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(f"[RATE_LIMIT] Rate limiting reset to initial state")
    
    def _update_conversation_token_estimate(self):
        """Update current_conversation_tokens by estimating tokens for all messages in conversation."""
        estimated_tokens = 0
        for msg in self.conversation:
            estimated_tokens += self._estimate_tokens(msg)
        self.state.current_conversation_tokens = estimated_tokens
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[TOKEN_ESTIMATE] Updated conversation token estimate: {estimated_tokens} tokens"
            )

    def _estimate_tokens(self, text_or_message):
        """Estimate token count for a string or message dict using tiktoken."""
        if self._token_encoder is None:
            # Default to cl100k_base (used by gpt-4, gpt-3.5-turbo)
            try:
                self._token_encoder = tiktoken.get_encoding("cl100k_base")
            except Exception:
                # Fallback to approximate estimation
                self._token_encoder = None
                # Use len//4 as fallback
                if isinstance(text_or_message, dict):
                    return len(str(text_or_message)) // 4
                else:
                    return len(str(text_or_message)) // 4
        
        if isinstance(text_or_message, dict):
            # Convert dict to JSON string for tokenization (more accurate for API)
            text = json.dumps(text_or_message)
        else:
            text = str(text_or_message)
        
        if self._token_encoder is not None:
            tokens = self._token_encoder.encode(text)
            return len(tokens)
        else:
            # Fallback when encoder not available
            return len(text) // 4

    def _estimate_request_tokens(self, messages, tool_definitions=None):
        """Estimate tokens for an API request including messages and tool definitions."""
        # Use provider's count_tokens method if available
        if hasattr(self.provider, 'count_tokens'):
            try:
                return self.provider.count_tokens(messages, tool_definitions)
            except Exception:
                pass
        
        # Fallback: estimate ourselves
        total_tokens = 0
        for msg in messages:
            total_tokens += self._estimate_tokens(msg)
        
        # Add tool definition tokens (crude estimate)
        if tool_definitions:
            # JSON stringify and estimate
            tools_json = json.dumps(tool_definitions)
            total_tokens += len(tools_json) // 4
        
        # Add some overhead for JSON structure, field names, etc.
        # OpenAI's actual token count includes JSON structure, field names, etc.
        # Add 10% overhead as rough estimate
        total_tokens = int(total_tokens * 1.1)
        
        return total_tokens

    def _get_model_context_window(self):
        """Get approximate context window size for the current model."""
        model = self.config.model.lower()
        
        # Common model context windows
        context_windows = {
            # OpenAI models
            "gpt-4": 8192,
            "gpt-4-32k": 32768,
            "gpt-4-turbo": 128000,
            "gpt-4o": 128000,
            "gpt-3.5-turbo": 16385,
            "gpt-3.5-turbo-16k": 16385,
            "gpt-3.5-turbo-instruct": 4096,
            # DeepSeek models
            "deepseek-reasoner": 128000,
            "deepseek-chat": 128000,
            "deepseek-coder": 128000,
            # StepFun models
            "step-3.5": 128000,
            # Anthropic models
            "claude-3-opus": 200000,
            "claude-3-sonnet": 200000,
            "claude-3-haiku": 200000,
            # Default fallback
            "default": 128000
        }
        
        # Check for exact match
        for key, window in context_windows.items():
            if key in model:
                return window
        
        # Check for partial matches
        if "gpt-4" in model:
            return 128000  # Most GPT-4 variants are 128k
        elif "gpt-3.5" in model:
            return 16385
        elif "claude" in model:
            return 200000
        elif "deepseek" in model:
            return 128000
        
        # Default to 128k for unknown models
        return 128000

    def _get_max_context_tokens(self) -> int:
        """Calculate maximum tokens available for context (input).
        
        Formula: model_context_window - safety_margin - response_tokens
        where response_tokens = runtime max_tokens or config max_tokens or DEFAULT_RESPONSE_TOKENS
        """
        model_context = self._get_model_context_window()
        # Use runtime max_tokens if set, else config, else default
        response_tokens = self.runtime_params.max_tokens or self.config.max_tokens or self.DEFAULT_RESPONSE_TOKENS
        max_context = model_context - self.SAFETY_MARGIN - response_tokens
        # Ensure at least some minimum context
        result = max(max_context, 1000)
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(f"[CONTEXT_CALC] model_context={model_context}, safety_margin={self.SAFETY_MARGIN}, response_tokens={response_tokens}, max_context={max_context}, result={result}")
        return result

    def _create_context_builder(self):
        """Create a ContextBuilder based on configuration."""
        from session.context_builder import LastNBuilder
        keep_last = self.config.max_history_turns
        if keep_last is None:
            keep_last = 100000  # effectively unlimited
        else:
            # Convert turns to messages: each turn may include user, assistant, tool messages
            # Use a conservative estimate of 4 messages per turn
            keep_last = keep_last * 4
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(f"[CONTEXT_BUILDER] Creating LastNBuilder with keep_last_messages={keep_last} (from max_history_turns={self.config.max_history_turns})")
        return LastNBuilder(keep_last_messages=keep_last, keep_system_prompt=True)

    def _load_system_prompt(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        possible_paths = [
            os.path.join(script_dir, "system_prompt.txt"),
            os.path.join(script_dir, "..", "system_prompt.txt"),
            "./system_prompt.txt"
        ]
        system_prompt = None
        for path in possible_paths:
            try:
                with open(path, "r") as f:
                    system_prompt = f.read()
                    break
            except FileNotFoundError:
                continue
        if system_prompt is None:
            raise RuntimeError("Could not find system_prompt.txt in any known location")
        return system_prompt
    
    def _ensure_system_prompt(self):
        if not any(msg.get("role") == "system" for msg in self.conversation):
            if self.config.system_prompt:
                system_prompt = self.config.system_prompt
            else:
                system_prompt = self._load_system_prompt()
            self.conversation.insert(0, {"role": "system", "content": system_prompt})

    def _add_to_conversation(self, message: Dict[str, Any]) -> None:
        """Add a message to the conversation, updating session timestamp if using a session."""
        self.conversation.append(message)
        if self.session is not None:
            self.session.updated_at = datetime.now()
            # Also append to session.user_history if it's not the same list
            if self.session.user_history is not self.conversation:
                self.session.user_history.append(message)

    def reset(self):
        self.conversation.clear()
        self._ensure_system_prompt()
        self.total_input_tokens = self.config.initial_input_tokens
        self.total_output_tokens = self.config.initial_output_tokens
        # Reset runtime params to defaults
        self.runtime_params = RuntimeParams(
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            top_p=None
        )
        # Reset state machine
        reset_events = self.state.reset()
        # Process any events from reset (though usually none for fresh reset)
        for event in reset_events:
            self._handle_state_event(event)
        
        # Update token estimate after resetting conversation and adding system prompt
        self._update_conversation_token_estimate()

    def update_runtime_params(self, **kwargs):
        """Update mutable runtime parameters (temperature, max_tokens, top_p)."""
        allowed = {'temperature', 'max_tokens', 'top_p'}
        for key, value in kwargs.items():
            if key not in allowed:
                raise ValueError(f"Unknown runtime parameter: {key}")
            setattr(self.runtime_params, key, value)
    def _handle_state_event(self, event):
        """Process a state event (e.g., token warning, turn warning).
        
        Events are dictionaries with 'type' field.
        For warning events, inject warning message into conversation.
        """
        if event.get("type") == "token_warning":
            # Note: token_warning events from AgentState have "message" field
            warning = event.get("message", event.get("warning", ""))
            sender = event.get("sender", "system")
            # Append warning as user message
            warning_msg = {"role": sender, "content": warning}
            self._add_to_conversation(warning_msg)
            # Estimate tokens for warning message and update state
            warning_tokens = self._estimate_tokens(warning_msg)
            self.state.current_conversation_tokens += warning_tokens
        elif event.get("type") == "turn_warning":
            # Note: turn_warning events from AgentState have "message" field
            warning = event.get("message", event.get("warning", ""))
            sender = event.get("sender", "system")
            warning_msg = {"role": sender, "content": warning}
            self._add_to_conversation(warning_msg)
            warning_tokens = self._estimate_tokens(warning_msg)
            self.state.current_conversation_tokens += warning_tokens
        elif event.get("type") in ("critical_countdown_start", "token_critical_active", "turn_critical_active"):
            # Handle countdown events - inject as system message
            message = event.get("message", "")
            sender = event.get("sender", "system")
            warning_msg = {"role": sender, "content": message}
            self._add_to_conversation(warning_msg)
            warning_tokens = self._estimate_tokens(warning_msg)
            self.state.current_conversation_tokens += warning_tokens
        elif event.get("type") == "execution_state_change":
            # Log execution state changes
            old_state = event.get("old_state")
            new_state = event.get("new_state")
            if self.logger:
                self.logger.py_logger.debug(
                    f"Execution state change: {old_state} -> {new_state}"
                )
        elif event.get("type") == "session_state_change":
            # Log session state changes
            old_state = event.get("old_state")
            new_state = event.get("new_state")
            if self.logger:
                self.logger.py_logger.debug(
                    f"Session state change: {old_state} -> {new_state}"
                )
        elif event.get("type") == "state_change":
            # Just log state changes for now
            if self.logger:
                self.logger.py_logger.debug(
                    f"State change: {event.get('old_state')} -> {event.get('new_state')}"
                )
    def submit_next_query(self, query: str):
        """Submit next query to paused agent."""
        self._next_query_queue.put(query)

    def request_reset(self):
        """Signal agent to reset on next iteration."""
        self._should_reset = True
        self._next_query_queue.put("[RESET]")

    def _wait_for_next_query(self):
        """Wait for next query to be submitted."""
        while self._paused:
            try:
                next_query = self._next_query_queue.get(timeout=0.1)
                if next_query == "[RESET]":
                    self.reset()
                    continue
                self._paused = False
                return next_query
            except queue.Empty:
                if self._should_reset:
                    self.reset()
                    self._should_reset = False
                    continue
    
    def _apply_summary_pruning(self, summary: str, keep_recent_turns: int):
        """Replace older conversation turns with a summary, keeping the most recent turns."""
        print(f"[DEBUG_PRUNING] _apply_summary_pruning called with summary length={len(summary)}, keep_recent_turns={keep_recent_turns}")
        print(f"[DEBUG_PRUNING] self.session exists: {self.session is not None}")
        if self.session:
            print(f"[DEBUG_PRUNING] session.user_history length: {len(self.session.user_history)}")
            print(f"[DEBUG_PRUNING] session.summary set: {self.session.summary is not None}")
        
        original_len = len(self.conversation)
        print(f"[DEBUG_PRUNING] original conversation length: {original_len}")
        
        # Use session history if available, otherwise agent conversation
        source_history = self.session.user_history if self.session is not None else self.conversation
        # Separate system messages and other messages from source history
        system_messages = []
        other_messages = []
        for msg in source_history:
            if msg.get("role") == "system":
                system_messages.append(msg)
            else:
                other_messages.append(msg)

        if not other_messages:
            return

        # Group messages into turns using shared logic
        turns = self._group_messages_into_turns(other_messages)
        print(f"[DEBUG_PRUNING] Grouped into {len(turns)} turns from {len(other_messages)} non-system messages")
        for i, turn in enumerate(turns):
            print(f"[DEBUG_PRUNING] Turn {i}: {[msg.get('role') for msg in turn]}")

        # Determine how many turns to keep from the end
        if keep_recent_turns <= 0:
            kept_turns = []
        else:
            kept_turns = turns[-keep_recent_turns:] if keep_recent_turns <= len(turns) else turns
        
        print(f"[DEBUG_PRUNING] Keeping {len(kept_turns)} turns (requested keep_recent_turns={keep_recent_turns}, total turns={len(turns)})")
        for i, turn in enumerate(kept_turns):
            print(f"[DEBUG_PRUNING] Kept turn {i}: {[msg.get('role') for msg in turn]}")
        
        # Create summary system message (truncated)
        MAX_SUMMARY_LENGTH = 4000
        truncated_summary = summary
        if len(truncated_summary) > MAX_SUMMARY_LENGTH:
            truncated_summary = truncated_summary[:MAX_SUMMARY_LENGTH] + "... (truncated)"
        # Compute pruning metadata
        if kept_turns:
            first_kept_turn_idx = len(turns) - len(kept_turns)
            discarded_msg_count = sum(len(turns[i]) for i in range(first_kept_turn_idx))
        else:
            discarded_msg_count = len(other_messages)
        insertion_idx = discarded_msg_count + len(system_messages)
        summary_msg = {
            "role": "system",
            "content": f"Summary of previous conversation: {truncated_summary}",
            "summary": True,
            "pruning_keep_recent_turns": keep_recent_turns,
            "pruning_discarded_msg_count": discarded_msg_count,
            "pruning_insertion_idx": insertion_idx,
        }
        
        # Store summary in session (if session exists)
        insertion_idx = None
        if self.session is not None:
            # DO NOT filter summary system messages from user_history - keep all messages
            # This ensures full history is preserved across pruning
            # filtered_history = []
            # for msg in self.session.user_history:
            #     if msg.get("role") == "system" and "Summary of previous conversation:" in msg.get("content", ""):
            #         continue
            #     filtered_history.append(msg)
            # self.session.user_history[:] = filtered_history
            
            # Store summary message in session.summary field
            self.session.summary = summary_msg
            self.session.updated_at = datetime.now()
            
            # Compute insertion position for logging (same as before)
            if kept_turns:
                first_kept_turn_idx = len(turns) - len(kept_turns)
                discarded_msg_count = 0
                for i in range(first_kept_turn_idx):
                    discarded_msg_count += len(turns[i])
                insertion_idx = discarded_msg_count + len(system_messages)
            else:
                insertion_idx = len(other_messages) + len(system_messages)
        
        # Flatten kept turns
        pruned_other = []
        for turn in kept_turns:
            pruned_other.extend(turn)

        # Clean up old system messages: keep only:
        # 1. Original system prompt (first system message if it looks like a prompt)
        # 2. Our new summary message
        # Discard all other system messages (old warnings, old summaries)
        cleaned_system_messages = []
        if system_messages:
            # Keep first system message (assumed to be main system prompt)
            cleaned_system_messages.append(system_messages[0])
        
        # Combine: cleaned system messages, summary message, then kept turns
        new_conversation = cleaned_system_messages + [summary_msg] + pruned_other
        self.conversation = new_conversation
        
        print(f"[DEBUG_PRUNING] Built new_conversation: {len(new_conversation)} messages")
        print(f"[DEBUG_PRUNING] cleaned_system_messages: {len(cleaned_system_messages)}")
        print(f"[DEBUG_PRUNING] kept turns: {len(kept_turns)}")
        print(f"[DEBUG_PRUNING] summary_msg content preview: {summary_msg.get('content', '')[:100]}")
        
        # Update session.user_history if session exists
        if self.session is not None:
            # Replace session.user_history with pruned conversation
            # This ensures the summary is persisted and reconstruction works
            self.session.user_history = new_conversation.copy()
            self.session.updated_at = datetime.now()
        
        new_len = len(self.conversation)
        if self.logger:
            self.logger.log_conversation_prune(original_len, new_len, "summary_pruning")
        
        # Debug logging
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[PRUNING] Applied summary pruning: kept {len(kept_turns)} turns, "
                f"removed {len(system_messages) - len(cleaned_system_messages)} old system messages, "
                f"conversation length: {len(self.conversation)} messages"
            )
            if self.session is not None:
                self.logger.py_logger.info(
                    f"[PRUNING] Updated session.user_history with pruned conversation ({len(new_conversation)} messages)"
                )
        
        # Update current conversation tokens estimate after pruning
        # We need to estimate because we won't get accurate token count until next API call
        old_token_count = self.state.current_conversation_tokens
        self._update_conversation_token_estimate()
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[PRUNING] Updated token estimate: {self.state.current_conversation_tokens} tokens (was {old_token_count})"
            )
        
        print(f"[DEBUG_PRUNING] _apply_summary_pruning completed. New conversation length: {len(self.conversation)} messages")
        if self.session:
            print(f"[DEBUG_PRUNING] session.user_history length after update: {len(self.session.user_history)}")
            print(f"[DEBUG_PRUNING] session.summary exists: {self.session.summary is not None}")
        else:
            print(f"[DEBUG_PRUNING] No session, only conversation updated.")
    
    def _group_messages_into_turns(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Group non-system messages into turns.
        
        Rules:
        - User messages always start a new turn
        - Assistant messages with tool_calls can also start a turn (after pruning)
        - All messages after a turn start belong to that turn until next user message
        - System messages should be filtered out before calling this method
        - Turns that don't start with user or assistant-with-tools are discarded
        
        This ensures tool call sequences stay together, even when they start with
        an assistant (due to pruning cutting off the user part of the turn).
        """
        turns = []
        current_turn = []
        
        # Debug logging
        import os
        debug = os.environ.get('DEBUG_TURN_GROUPING')
        if debug:
            print(f"[DEBUG_TURN_GROUPING] Grouping {len(messages)} messages")
            for i, msg in enumerate(messages):
                role = msg.get("role")
                content_preview = str(msg.get("content", ""))[:50]
                has_tool_calls = "tool_calls" in msg and msg["tool_calls"]
                print(f"  [{i}] {role}: {content_preview}... tool_calls={has_tool_calls}")
        
        for msg in messages:
            role = msg.get("role")
            
            # Skip system messages (should have been filtered out)
            if role == "system":
                continue
                
            if role == "user":
                # User always starts a new turn
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            elif role == "assistant" and msg.get("tool_calls"):
                # Assistant with tool_calls can start a turn (after pruning)
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            else:
                # Other messages (assistant without tools, tool) - add to current turn
                if current_turn:
                    current_turn.append(msg)
                else:
                    # Orphaned message without user or assistant-with-tools
                    # Discard it
                    if debug:
                        print(f"[DEBUG_TURN_GROUPING] Discarding orphaned {role} message")
                    continue
        
        if current_turn:
            turns.append(current_turn)
        
        # Filter to keep only valid turns
        valid_turns = []
        for turn in turns:
            if not turn:
                continue
            first_msg = turn[0]
            first_role = first_msg.get("role")
            
            if first_role == "user":
                valid_turns.append(turn)
            elif first_role == "assistant" and first_msg.get("tool_calls"):
                # Turn starts with assistant that made tool calls
                # This is valid (e.g., after pruning cut off the user)
                valid_turns.append(turn)
            elif debug:
                print(f"[DEBUG_TURN_GROUPING] Discarding turn starting with {first_role}")
        
        if debug:
            print(f"[DEBUG_TURN_GROUPING] Returned {len(valid_turns)} valid turns")
            for i, turn in enumerate(valid_turns):
                print(f"  Turn {i}: {[msg.get('role') for msg in turn]}")
        
        return valid_turns
    
    def _reconstruct_pruned_conversation_from_session(self):
        """Reconstruct agent's conversation from session history: sysprompt + summary + recent turns."""
        if self.session is None:
            return
        
        # Find system messages in conversation
        system_messages = [msg for msg in self.conversation if msg.get("role") == "system"]
        if not system_messages:
            return
        
        # Identify main system prompt (first system message)
        main_prompt = system_messages[0]
        
        # Try to get summary from session.summary field first (new format)
        summary_msg = None
        summary_idx = -1
        
        # First, find ALL summaries in conversation (scan from end to find most recent)
        all_summaries = []
        for i, msg in enumerate(self.conversation):
            if msg.get("role") == "system" and "Summary of previous conversation:" in msg.get("content", ""):
                all_summaries.append((i, msg))
        
        # If we have summaries, use the last one (most recent)
        if all_summaries:
            summary_idx, summary_msg = all_summaries[-1]
            # Update session.summary to match if it's different
            if self.session.summary != summary_msg:
                self.session.summary = summary_msg
        elif self.session.summary is not None:
            # Session has separate summary field but no summary in conversation
            summary_msg = self.session.summary
            summary_idx = -1  # Not in conversation
        else:
            # No summary found
            summary_msg = None
            summary_idx = -1
        
        # Determine which messages to keep after summary
        # Use pruning metadata if available, otherwise use config.max_history_turns
        keep_recent_turns = None
        if summary_msg and "pruning_keep_recent_turns" in summary_msg:
            keep_recent_turns = summary_msg.get("pruning_keep_recent_turns")
        
        # Default to config value if no metadata
        if keep_recent_turns is None:
            keep_recent_turns = self.config.max_history_turns if self.config.max_history_turns else 100000
        
        if summary_msg is not None and summary_idx >= 0:
            # We have a summary in the conversation
            # Keep messages from summary_idx onward (including summary)
            new_conversation = [main_prompt]
            if summary_msg is not main_prompt:
                new_conversation.append(summary_msg)
            
            # Get messages after summary and group them into turns
            messages_after_summary = self.conversation[summary_idx + 1:]
            # Filter out system messages before grouping
            non_system_after = [msg for msg in messages_after_summary if msg.get("role") != "system"]
            turns_after_summary = self._group_messages_into_turns(non_system_after)
            print(f"[DEBUG_RECONSTRUCT] Found {len(turns_after_summary)} turns after summary (keep_recent_turns={keep_recent_turns})")
            
            # Keep only the most recent keep_recent_turns turns
            if keep_recent_turns < len(turns_after_summary):
                print(f"[DEBUG_RECONSTRUCT] Keeping {keep_recent_turns} most recent turns (discarding {len(turns_after_summary) - keep_recent_turns} older turns)")
                turns_after_summary = turns_after_summary[-keep_recent_turns:]
            else:
                print(f"[DEBUG_RECONSTRUCT] Keeping all {len(turns_after_summary)} turns (keep_recent_turns >= total turns)")
            
            # Flatten kept turns
            for turn in turns_after_summary:
                new_conversation.extend(turn)
            
            self.conversation = new_conversation
            
        elif summary_msg is not None and summary_idx == -1:
            # Summary exists in session.summary but not in conversation
            # This is the new format: we need to reconstruct based on pruning metadata
            new_conversation = [main_prompt, summary_msg]
            
            # Use pruning_insertion_idx if available to separate pre/post-summary messages
            insertion_idx = summary_msg.get("pruning_insertion_idx")
            if insertion_idx is not None and 0 <= insertion_idx < len(self.conversation):
                # Messages at or after insertion_idx are after the summary insertion point
                # Messages before insertion_idx were summarized (pre-summary)
                messages_after_summary = self.conversation[insertion_idx:]
                # Filter out system messages from post-summary messages
                non_system_post = [msg for msg in messages_after_summary if msg.get("role") != "system"]
                turns = self._group_messages_into_turns(non_system_post)
                print(f"[DEBUG_RECONSTRUCT] Using insertion_idx={insertion_idx}, found {len(turns)} turns after summary")
            else:
                # Fallback: get all non-system messages and group them into turns
                non_system = [msg for msg in self.conversation if msg.get("role") != "system"]
                turns = self._group_messages_into_turns(non_system)
                print(f"[DEBUG_RECONSTRUCT] No insertion_idx, using all {len(turns)} turns from conversation")
            
            # Keep only the most recent keep_recent_turns turns
            print(f"[DEBUG_RECONSTRUCT] Second case: have {len(turns)} turns, keep_recent_turns={keep_recent_turns}")
            if keep_recent_turns < len(turns):
                print(f"[DEBUG_RECONSTRUCT] Keeping {keep_recent_turns} most recent turns (discarding {len(turns) - keep_recent_turns} older turns)")
                turns = turns[-keep_recent_turns:]
            else:
                print(f"[DEBUG_RECONSTRUCT] Keeping all {len(turns)} turns (keep_recent_turns >= total turns)")
            
            # Flatten kept turns
            for turn in turns:
                new_conversation.extend(turn)
            
            self.conversation = new_conversation
            
        else:
            # No summary found, just keep recent turns with main prompt
            non_system = [msg for msg in self.conversation if msg.get("role") != "system"]
            turns = self._group_messages_into_turns(non_system)
            print(f"[DEBUG_RECONSTRUCT] No summary case: have {len(turns)} turns, keep_recent_turns={keep_recent_turns}")
            
            # Keep only the most recent keep_recent_turns turns
            if keep_recent_turns < len(turns):
                print(f"[DEBUG_RECONSTRUCT] Keeping {keep_recent_turns} most recent turns (discarding {len(turns) - keep_recent_turns} older turns)")
                turns = turns[-keep_recent_turns:]
            else:
                print(f"[DEBUG_RECONSTRUCT] Keeping all {len(turns)} turns (keep_recent_turns >= total turns)")
            
            # Flatten kept turns
            new_conversation = [main_prompt]
            for turn in turns:
                new_conversation.extend(turn)
            
            self.conversation = new_conversation
        
        # Log reconstruction
        if self.logger and hasattr(self.logger, 'py_logger'):
            self.logger.py_logger.info(
                f"[RECONSTRUCT] Reconstructed pruned conversation: {len(self.conversation)} messages, "
                f"summary_found={summary_msg is not None}, summary_idx={summary_idx}, keep_recent_turns={keep_recent_turns}"
            )

    def _format_tokens(self, tokens):
        """Format token count in thousands with 'k' suffix."""
        if tokens >= 1000:
            return f"{tokens // 1000}k"
        return str(tokens)
    

    def process_query(self, query):
        """Process a user query, appending it to conversation and running the agent.
        Yields events as dicts."""
        # Ensure system prompt present
        self._ensure_system_prompt()
        
        # Update execution state: use intermediate STARTING state when starting fresh
        current_exec_state = self.state.execution_state
        if current_exec_state == ExecutionState.RUNNING:
            # This shouldn't happen, but handle gracefully
            if self.logger:
                self.logger.log_error("EXECUTION_STATE", "process_query called while already RUNNING")
        elif current_exec_state in (ExecutionState.PAUSED, ExecutionState.WAITING_FOR_USER):
            # Resuming from pause or user interaction - go directly to RUNNING
            events = self.state.set_execution_state(ExecutionState.RUNNING)
            for event in events:
                self._handle_state_event(event)
        else:
            # Starting from IDLE, STOPPED, etc. - use STARTING intermediate state
            events = self.state.set_execution_state(ExecutionState.STARTING)
            for event in events:
                self._handle_state_event(event)
            # Immediately transition to RUNNING
            events = self.state.set_execution_state(ExecutionState.RUNNING)
            for event in events:
                self._handle_state_event(event)
        
        # Log agent start if logger exists
        if self.logger:
            config_data = {
                "model": self.config.model,
                "temperature": self.config.temperature,
                "max_turns": self.config.max_turns,
                "max_history_turns": self.config.max_history_turns,
                "max_tokens": self.config.max_tokens,
                "keep_initial_query": self.config.keep_initial_query,
                "keep_system_messages": self.config.keep_system_messages,
            }
            self.logger.log_agent_start(query, config_data)
        # Append user message
        user_msg = {"role": "user", "content": query}
        self._add_to_conversation(user_msg)
        # Estimate tokens for the new user message (including JSON structure) and update current count
        estimated_tokens = self._estimate_tokens(user_msg)
        self.state.current_conversation_tokens += estimated_tokens
        
        prev_conversation_len = len(self.conversation)
        last_input_tokens = 0
        last_output_tokens = 0
        
        for turn in range(self.config.max_turns):
            # Log turn start
            if self.logger:
                self.logger.log_turn_start(turn)
            
            # Check stop signal
            if self.stop_check and self.stop_check():
                # Update execution state: transition through STOPPING intermediate state
                events = self.state.set_execution_state(ExecutionState.STOPPING)
                for event in events:
                    self._handle_state_event(event)
                # Then transition to STOPPED
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    self._handle_state_event(event)

                if self.logger:
                    self.logger.log_stop_signal()
                    self.logger.log_agent_end("stopped", "Stop signal received")
                    self.logger.close()
                yield {
                    "type": "stopped",
                    "turn": turn,
                    "context_length": self.state.current_conversation_tokens,
                    "history": self.session.user_history.copy() if self.session is not None else self.conversation.copy(),
                    "usage": {"input": last_input_tokens, "output": last_output_tokens,
                              "total_input": self.total_input_tokens, "total_output": self.total_output_tokens}
                }
                return            
            # Turn monitoring warning
            turn_events = self.state.update_turn_state(turn)
            for event in turn_events:
                if event["type"] == "turn_warning":
                    # Add warning message to conversation as user message
                    warning_msg = {"role": "user", "content": event["message"]}
                    self._add_to_conversation(warning_msg)
                    # Update token count for warning message
                    warning_tokens = self._estimate_tokens(warning_msg)
                    self.state.current_conversation_tokens += warning_tokens
                # Yield event with usage info
                yield {
                    "type": event["type"],
                    "message": event["message"],
                    "turn_count": event.get("turn_count", turn),
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }

            # Update conversation token estimate from scratch to prevent drift
            self._update_conversation_token_estimate()
            # Token monitoring warning
            token_events = self.state.update_token_state(self.state.current_conversation_tokens)
            for event in token_events:
                if event["type"] == "token_warning":
                    # Add warning message to conversation as user message
                    warning_msg = {"role": "user", "content": event["message"]}
                    self._add_to_conversation(warning_msg)
                    # Update token count for warning message
                    warning_tokens = self._estimate_tokens(warning_msg)
                    self.state.current_conversation_tokens += warning_tokens
                # Yield event with usage info
                yield {
                    "type": event["type"],
                    "message": event["message"],
                    "token_count": event.get("token_count", self.state.current_conversation_tokens),
                    "context_length": self.state.current_conversation_tokens,
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }

            
            # Ensure any assistant message with tool_calls has reasoning_content field
            for msg in self.conversation:
                if msg.get("role") == "assistant" and "tool_calls" in msg:
                    if msg.get("reasoning_content") is None:
                        msg["reasoning_content"] = ""
            
            # Log conversation state before building context
            if self.logger and hasattr(self.logger, 'py_logger'):
                system_msgs = [msg for msg in self.conversation if msg.get("role") == "system"]
                self.logger.py_logger.info(f"[CONVERSATION] Total messages: {len(self.conversation)}, system messages: {len(system_msgs)}")
            
            # Build the context for the LLM using the configured builder
            # Calculate maximum tokens available for context (input)
            max_context_tokens = self._get_max_context_tokens()
            if self.logger and hasattr(self.logger, 'py_logger'):
                self.logger.py_logger.info(f"[CONTEXT] Max context tokens: {max_context_tokens}, model: {self.config.model}")
            # DEBUG: Print conversation before building context
            print("\n=== CONVERSATION BEFORE CONTEXT BUILDER ===")
            for i, msg in enumerate(self.conversation):
                role = msg.get('role', 'NO_ROLE')
                content_preview = str(msg.get('content', ''))[:80].replace('\n', ' ')
                if role == 'tool':
                    tool_call_id = msg.get('tool_call_id', 'NO_ID')
                    print(f"  [{i}] {role}: tool_call_id={tool_call_id}, content={content_preview}...")
                elif role == 'assistant':
                    tool_calls = msg.get('tool_calls')
                    if tool_calls:
                        print(f"  [{i}] {role}: HAS TOOL_CALLS {len(tool_calls)}, content={content_preview}...")
                    else:
                        print(f"  [{i}] {role}: {content_preview}...")
                else:
                    print(f"  [{i}] {role}: {content_preview}...")
            print("=== END DEBUG ===\n")
            messages = self.context_builder.build(self.conversation, max_tokens=max_context_tokens)
            if self.logger and hasattr(self.logger, 'py_logger'):
                # Estimate token count for messages
                import tiktoken
                try:
                    encoder = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    encoder = None
                total_tokens = sum(self.context_builder._estimate_tokens(msg, encoder) for msg in messages)
                self.logger.py_logger.info(f"[CONTEXT] Built context: {len(messages)} messages, ~{total_tokens} tokens")
            
            # Log LLM request
            if self.logger:
                self.logger.log_llm_request(messages, self.tool_definitions)
            
            # Apply rate limiting delay if active
            if self.rate_limit_active:
                delay = min(self.rate_limit_delay, self.rate_limit_max_wait)
                if delay > 0:
                    if self.logger and hasattr(self.logger, 'py_logger'):
                        self.logger.py_logger.info(f"[RATE_LIMIT] Applying rate limit delay: {delay}s between turns")
                    import time
                    time.sleep(delay)
            
            # Call LLM provider
            formatted_tools = self.provider.format_tools(self.tool_definitions)

            # Estimate request tokens and check against model context window
            request_tokens = self._estimate_request_tokens(messages, formatted_tools)
            # Get model context window (approximate)
            model_context_window = self._get_model_context_window()
            critical_threshold = int(model_context_window * 0.95)  # 95% of context window
            warning_threshold = int(model_context_window * 0.85)  # 85% of context window
            
            if request_tokens > model_context_window:
                # Cannot proceed - request exceeds model context window
                error = f"[SYSTEM] Request token count ({request_tokens}) exceeds model context window ({model_context_window}). Cannot make API call. Please use SummarizeTool to reduce context size."
                error_msg = {"role": "user", "content": error}
                self._add_to_conversation(error_msg)
                error_tokens = self._estimate_tokens(error_msg)
                self.state.current_conversation_tokens += error_tokens
                # Yield error event
                yield {
                    "type": "token_warning",
                    "message": error,
                    "token_count": request_tokens,
                    "state": "critical",
                    "request_tokens": request_tokens,
                    "model_context_window": model_context_window
                }
                # Still attempt API call but it will likely fail
                if self.logger:
                    self.logger.log_token_warning(
                        "low", "critical", request_tokens,
                        f"Request tokens {request_tokens} exceed model context window {model_context_window}"
                    )
            elif request_tokens > critical_threshold:
                # Critical warning - near context limit
                warning = f"[SYSTEM] Request token count ({request_tokens}) is near model context window limit ({model_context_window}). Please use SummarizeTool immediately to reduce context size."
                warning_msg = {"role": "user", "content": warning}
                self._add_to_conversation(warning_msg)
                warning_tokens = self._estimate_tokens(warning_msg)
                self.state.current_conversation_tokens += warning_tokens
                # Yield token warning event
                yield {
                    "type": "token_warning",
                    "message": warning,
                    "token_count": request_tokens,
                    "state": "critical",
                    "request_tokens": request_tokens,
                    "model_context_window": model_context_window
                }
                if self.logger:
                    self.logger.log_token_warning(
                        "low", "critical", request_tokens,
                        f"Request tokens {request_tokens} near model context window {model_context_window}"
                    )
            elif request_tokens > warning_threshold:
                # Warning - approaching context limit
                warning = f"[SYSTEM] Request token count ({request_tokens}) is approaching model context window ({model_context_window}). Consider using SummarizeTool soon."
                warning_msg = {"role": "user", "content": warning}
                self._add_to_conversation(warning_msg)
                warning_tokens = self._estimate_tokens(warning_msg)
                self.state.current_conversation_tokens += warning_tokens
                # Yield token warning event
                yield {
                    "type": "token_warning",
                    "message": warning,
                    "token_count": request_tokens,
                    "state": "warning",
                    "request_tokens": request_tokens,
                    "model_context_window": model_context_window
                }
                if self.logger:
                    self.logger.log_token_warning(
                        "low", "warning", request_tokens,
                        f"Request tokens {request_tokens} approaching model context window {model_context_window}"
                    )
            
            try:
                # Build chat kwargs using runtime parameters (overrides config)
                chat_kwargs = {
                    "temperature": self.runtime_params.temperature,
                }
                if self.runtime_params.max_tokens is not None:
                    chat_kwargs["max_tokens"] = self.runtime_params.max_tokens
                if self.runtime_params.top_p is not None:
                    chat_kwargs["top_p"] = self.runtime_params.top_p
                # DEBUG: Print messages being sent to API
                print("\n=== CHAT COMPLETION MESSAGES ===")
                for i, msg in enumerate(messages):
                    role = msg.get('role', 'NO_ROLE')
                    content_preview = str(msg.get('content', ''))[:80].replace('\n', ' ')
                    if role == 'tool':
                        tool_call_id = msg.get('tool_call_id', 'NO_ID')
                        print(f"  [{i}] {role}: tool_call_id={tool_call_id}, content={content_preview}...")
                    elif role == 'assistant':
                        tool_calls = msg.get('tool_calls')
                        if tool_calls:
                            print(f"  [{i}] {role}: HAS TOOL_CALLS {len(tool_calls)}, content={content_preview}...")
                        else:
                            print(f"  [{i}] {role}: {content_preview}...")
                    else:
                        print(f"  [{i}] {role}: {content_preview}...")
                print("=== END DEBUG ===\n")
                
                response = self.provider.chat_completion(
                    messages=messages,
                    tools=formatted_tools,
                    **chat_kwargs
                )
                
                # Debug: Print raw response if environment variable is set
                import os
                if os.environ.get('DEBUG_RAW_RESPONSE'):
                    raw = str(response.raw_response)
                    if len(raw) > 1000:
                        raw = raw[:1000] + f"... (truncated, total {len(raw)} chars)"
                    print(f"[DEBUG_RAW_RESPONSE] Raw response type: {type(response.raw_response)}")
                    print(f"[DEBUG_RAW_RESPONSE] Raw response: {raw}")
            except RateLimitExceeded as e:
                # Handle rate limit errors with exponential backoff
                import os
                if os.environ.get('DEBUG_RAW_RESPONSE'):
                    print(f"[DEBUG_RAW_RESPONSE_ERROR] RateLimitExceeded: {e}")
                    # Try to get raw response if available
                    if hasattr(e, 'raw_response'):
                        raw = str(e.raw_response)
                        if len(raw) > 1000:
                            raw = raw[:1000] + f"... (truncated, total {len(raw)} chars)"
                        print(f"[DEBUG_RAW_RESPONSE_ERROR] Raw error response: {raw}")
                    else:
                        print(f"[DEBUG_RAW_RESPONSE_ERROR] No raw_response attribute in error")
                
                # Increment rate limit count and calculate delays
                self.rate_limit_count += 1
                self.rate_limit_active = True
                
                # Increase delay for future turns (exponential backoff)
                if self.rate_limit_count > 1:
                    self.rate_limit_delay = min(
                        self.rate_limit_delay * self.rate_limit_backoff_factor,
                        self.rate_limit_max_wait
                    )
                
                # Initial wait (10 seconds)
                wait_time = self.rate_limit_base_wait
                if self.logger:
                    self.logger.log_error("RATE_LIMIT", f"Rate limit exceeded, waiting {wait_time}s, delay between turns: {self.rate_limit_delay}s")
                
                # Yield rate limit warning event
                yield {
                    "type": "rate_limit_warning",
                    "message": f"Rate limit exceeded. Waiting {wait_time}s before retrying. Delay between turns: {self.rate_limit_delay}s",
                    "wait_time": wait_time,
                    "turn_delay": self.rate_limit_delay,
                    "rate_limit_count": self.rate_limit_count,
                    "turn": turn,
                }
                
                # Wait the initial wait time
                import time
                time.sleep(wait_time)
                
                # Continue to next turn with delay between turns
                break
                
            except ProviderError as e:
                # Handle other provider errors (non-rate-limit)
                import os
                if os.environ.get('DEBUG_RAW_RESPONSE'):
                    print(f"[DEBUG_RAW_RESPONSE_ERROR] ProviderError: {e}")
                    # Try to get raw response if available
                    if hasattr(e, 'raw_response'):
                        raw = str(e.raw_response)
                        if len(raw) > 1000:
                            raw = raw[:1000] + f"... (truncated, total {len(raw)} chars)"
                        print(f"[DEBUG_RAW_RESPONSE_ERROR] Raw error response: {raw}")
                    else:
                        print(f"[DEBUG_RAW_RESPONSE_ERROR] No raw_response attribute in error")
                # Update execution state: transition through STOPPING intermediate state
                events = self.state.set_execution_state(ExecutionState.STOPPING)
                for event in events:
                    self._handle_state_event(event)
                # Then transition to STOPPED
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    self._handle_state_event(event)
                if self.logger:
                    self.logger.log_error("PROVIDER_ERROR", str(e))
                    self.logger.log_agent_end("provider_error", f"Provider error: {e}")
                    self.logger.close()
                yield {
                    "type": "error",
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                    "turn": turn,
                    "context_length": self.state.current_conversation_tokens,
                    "history": self.session.user_history.copy() if self.session is not None else self.conversation.copy(),
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                return
            except Exception as e:
                # Catch any other unexpected exception
                print(f"[Agent] Unexpected exception in process_query: {e}")
                traceback.print_exc()
                # Update execution state: transition through STOPPING intermediate state
                events = self.state.set_execution_state(ExecutionState.STOPPING)
                for event in events:
                    self._handle_state_event(event)
                # Then transition to STOPPED
                events = self.state.set_execution_state(ExecutionState.STOPPED)
                for event in events:
                    self._handle_state_event(event)
                if self.logger:
                    self.logger.log_error("UNEXPECTED_ERROR", str(e))
                    self.logger.log_agent_end("unexpected_error", f"Unexpected error: {e}")
                    self.logger.close()
                yield {
                    "type": "error",
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                    "turn": turn,
                    "context_length": self.state.current_conversation_tokens,
                    "history": self.session.user_history.copy() if self.session is not None else self.conversation.copy(),
                    "usage": {
                        "input": last_input_tokens,
                        "output": last_output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                return
            
            # Token usage
            usage = response.usage
            if usage:
                input_tokens = usage.get("prompt_tokens", 0) or 0
                output_tokens = usage.get("completion_tokens", 0) or 0
                has_accurate_usage = True
            else:
                input_tokens = output_tokens = 0
                has_accurate_usage = False
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            # Reset rate limit count on successful LLM call (but keep delay and active flag)
            if self.rate_limit_count > 0:
                self.rate_limit_count = 0
                if self.logger and hasattr(self.logger, 'py_logger'):
                    self.logger.py_logger.info(f"[RATE_LIMIT] Successful LLM call, reset rate limit count to 0 (delay remains: {self.rate_limit_delay}s)")
            
            # Gradually reduce delay after successful calls when rate limiting was active
            if self.rate_limit_active and self.rate_limit_delay > 1.0:
                # Reduce delay by 10% each successful turn, down to minimum of 1.0
                new_delay = max(self.rate_limit_delay * 0.9, 1.0)
                if new_delay < self.rate_limit_delay:
                    self.rate_limit_delay = new_delay
                    if self.logger and hasattr(self.logger, 'py_logger'):
                        self.logger.py_logger.info(f"[RATE_LIMIT] Reducing turn delay to {self.rate_limit_delay:.2f}s after successful call")
                # If delay is back to 1.0, we're no longer rate limited
                if self.rate_limit_delay <= 1.0:
                    self.rate_limit_active = False
                    if self.logger and hasattr(self.logger, 'py_logger'):
                        self.logger.py_logger.info(f"[RATE_LIMIT] Rate limiting deactivated (delay back to 1.0s)")
            
            if has_accurate_usage:
                # Accurate token counts available from provider
                # Note: input_tokens + output_tokens represents tokens for this API call
                # We don't overwrite current_conversation_tokens to maintain accumulation
                pass
            # Otherwise, keep current conversation tokens (estimated)
            last_input_tokens = input_tokens
            last_output_tokens = output_tokens

            # Extract assistant message from normalized response
            content = response.content or ""
            reasoning = response.reasoning
            tool_calls = response.tool_calls  # Already in normalized format            
            # Log LLM response
            if self.logger:
                usage_dict = {
                    "input": input_tokens,
                    "output": output_tokens,
                    "total_input": self.total_input_tokens,
                    "total_output": self.total_output_tokens,
                }
                tool_calls_dict = None
                if tool_calls:
                    tool_calls_dict = tool_calls
                self.logger.log_llm_response(content, reasoning, tool_calls_dict, usage_dict, response.raw_response)
            
            # Build assistant message dict for storage
            assistant_dict = {"role": "assistant", "content": content}
            if reasoning is not None:
                assistant_dict["reasoning_content"] = reasoning
            elif tool_calls:
                assistant_dict["reasoning_content"] = ""
            if tool_calls:
                assistant_dict["tool_calls"] = tool_calls
            
            self._add_to_conversation(assistant_dict)
            # Estimate tokens for assistant message
            assistant_tokens = self._estimate_tokens(assistant_dict)
            if has_accurate_usage:
                # Use accurate output token count from provider
                self.state.current_conversation_tokens += output_tokens
            else:
                # Use estimated assistant tokens
                self.state.current_conversation_tokens += assistant_tokens
            
            if self.logger:
                self.logger.log_conversation_update(self.conversation, "append_assistant")
            
            # If there are tool calls, execute them and append tool responses
            if tool_calls:
                executed_tools = []
                final_detected = False
                final_content = None
                user_interaction_requested = False
                user_interaction_message = None
                summary_requested = False
                summary_text = None
                summary_keep_recent_turns = 0
                
                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"]
                    
                    # Check if tool is allowed in current state
                    if not self.state.is_tool_allowed(tool_name):
                        allowed_tools = self.state.get_allowed_tools()
                        if allowed_tools:
                            tool_result = f'''❌ TOOL CALL REJECTED ❌

You attempted to use '{tool_name}', which is currently FORBIDDEN.

Current state: CRITICAL token countdown expired (restrictions active)
REQUIRED ACTION: SummarizeTool
Why: Token limit exceeded - conversation must be pruned before continuing.

You may call:
- SummarizeTool (to prune and continue)
- Final (to end conversation)
- FinalReport (to end with report)

Call SummarizeTool NOW to proceed.'''
                        else:
                            tool_result = f'''❌ TOOL CALL REJECTED ❌

You attempted to use '{tool_name}', which is currently FORBIDDEN.

Current state: token_state={self.state.token_state.value}, turn_state={self.state.turn_state.value}
Possible reasons: Token or turn limits exceeded with active restrictions.

Check system warnings for required actions.'''
                        
                        # Append tool result with error
                        self._add_to_conversation({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": tool_result
                        })
                        # Estimate tokens for tool result
                        tool_tokens = len(str(tool_result)) // 4
                        self.state.current_conversation_tokens += tool_tokens
                        
                        executed_tools.append({
                            "name": tool_name,
                            "arguments": {},
                            "result": tool_result
                        })
                        continue
                    
                    arguments_str = tool_call["function"]["arguments"]
                    
                    try:
                        arguments = json.loads(arguments_str)
                    except json.JSONDecodeError:
                        try:
                            arguments = repair_loads(arguments_str)
                            if self.logger:
                                self.logger.py_logger.info(f"JSON repaired for {tool_name}")
                        except Exception as e:
                            tool_result = f"Invalid JSON in arguments: {e}. Raw: {arguments_str}"
                            if self.logger:
                                self.logger.log_error("JSON_DECODE_ERROR", f"Failed to parse JSON for {tool_name}: {e}")
                            self._add_to_conversation({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": tool_result
                            })
                            executed_tools.append({
                                "name": tool_name,
                                "arguments": {"error": "Invalid JSON", "raw": arguments_str},
                                "result": tool_result
                            })
                            continue
                    
                    # Log tool call
                    if self.logger:
                        self.logger.log_tool_call(tool_name, arguments, tool_call["id"])
                    
                    # Find matching tool class
                    tool_class = next((cls for cls in self.tool_classes if cls.__name__ == tool_name), None)
                    if not tool_class:
                        error_msg = f"Unknown tool: {tool_name}"
                        tool_result = error_msg
                    else:
                        try:
                            # Add workspace_path from config if tool supports it
                            tool_args = arguments.copy()
                            if self.config.workspace_path is not None:
                                tool_args['workspace_path'] = self.config.workspace_path
                            if self.config.tool_output_token_limit is not None:
                                tool_args['token_limit'] = self.config.tool_output_token_limit

                            # Security capability check
                            if SECURITY_AVAILABLE:
                                try:
                                    CapabilityRegistry.check(id(self), tool_name, **tool_args)
                                except Exception as e:
                                    tool_result = f"Security check failed: {e}"
                                    raise
                            tool_instance = tool_class(**tool_args)
                            tool_result = tool_instance.execute()
                            # Check if this is a Final tool
                            if isinstance(tool_instance, Final):
                                final_detected = True
                                final_content = tool_result
                            # Check if this is a RequestUserInteraction tool
                            if isinstance(tool_instance, RequestUserInteraction):
                                user_interaction_requested = True
                                user_interaction_message = tool_result
                            # Check if this is a SummarizeTool
                            if isinstance(tool_instance, SummarizeTool):
                                summary_requested = True
                                summary_text = tool_instance.summary
                                summary_keep_recent_turns = tool_instance.keep_recent_turns
                        except ValidationError as e:
                            tool_result = f"Invalid arguments: {e}"
                        except Exception as e:
                            tool_result = f"Error executing tool: {e}"
                    
                    # Log tool result
                    if self.logger:
                        self.logger.log_tool_result(tool_name, tool_result, tool_call["id"])
                    
                    # Append tool result
                    self._add_to_conversation({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": tool_result
                    })
                    # Estimate tokens for tool result
                    tool_tokens = len(str(tool_result)) // 4
                    self.state.current_conversation_tokens += tool_tokens
                    
                    if self.logger:
                        self.logger.log_conversation_update(self.conversation, "append_tool_result")
                    
                    executed_tools.append({
                        "name": tool_name,
                        "arguments": arguments,
                        "result": tool_result
                    })

                # Decrement critical countdowns after tools have executed
                countdown_events = self.state.decrement_critical_countdown()
                for event in countdown_events:
                    self._handle_state_event(event)

                # Apply summary pruning if requested
                if summary_requested:
                    self._apply_summary_pruning(summary_text, summary_keep_recent_turns)
                    # Yield system event for GUI to display summary
                    yield {
                        "type": "system",
                        "content": f"Summary pruning applied: kept {summary_keep_recent_turns} recent turns",
                        "message": f"Summary pruning applied: kept {summary_keep_recent_turns} recent turns",
                        "summary": summary_text,
                        "turns_kept": summary_keep_recent_turns,
                        "context_length": self.state.current_conversation_tokens,
                        "usage": {
                            "input": input_tokens,
                            "output": output_tokens,
                            "total_input": self.total_input_tokens,
                            "total_output": self.total_output_tokens,
                        }
                    }
                # Log turn completion
                if self.logger:
                    turn_usage = {"input": input_tokens, "output": output_tokens}
                    self.logger.log_turn_complete(turn, turn_usage)
                    current_len = len(self.conversation)
                    if current_len < prev_conversation_len:
                        if self.logger and hasattr(self.logger, 'py_logger'):
                            self.logger.py_logger.warning(f"[WARNING] Conversation length decreased within turn {turn}: {prev_conversation_len} -> {current_len}")
                prev_conversation_len = len(self.conversation)
                
                # Yield turn event
                yield {
                    "type": "turn",
                    "turn": turn,
                    "assistant_content": content,
                    "tool_calls": executed_tools,
                    "reasoning": reasoning,
                    "context_length": self.state.current_conversation_tokens,
                    "history": self.session.user_history.copy() if self.session is not None else self.conversation.copy(),
                    "usage": {
                        "input": input_tokens,
                        "output": output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                
                # If a RequestUserInteraction tool was called, stop the agent and wait for user response
                if user_interaction_requested:
                    # Update execution state: transition to WAITING_FOR_USER
                    events = self.state.set_execution_state(ExecutionState.WAITING_FOR_USER)
                    for event in events:
                        self._handle_state_event(event)
                    
                    if self.logger:
                        self.logger.log_user_interaction_requested(user_interaction_message)
                        self.logger.log_agent_end("user_interaction_requested", "Waiting for user response")
                        self.logger.close()
                    yield {
                        "type": "user_interaction_requested",
                        "turn": turn,
                        "context_length": self.state.current_conversation_tokens,
                        "message": user_interaction_message,
                        "history": self.session.user_history.copy() if self.session is not None else self.conversation.copy(),
                        "usage": {
                            "input": input_tokens,
                            "output": output_tokens,
                            "total_input": self.total_input_tokens,
                            "total_output": self.total_output_tokens,
                        }
                    }
                    return
                
                # If a Final tool was called, stop the agent
                if final_detected:
                    # Update execution state: transition to FINALIZED
                    events = self.state.set_execution_state(ExecutionState.FINALIZED)
                    for event in events:
                        self._handle_state_event(event)
                    
                    if self.logger:
                        self.logger.log_final_detected(final_content)
                        self.logger.log_agent_end("final", "Final tool executed", final_content)
                        self.logger.close()
                    yield {
                        "type": "final",
                        "content": final_content,
                        "context_length": self.state.current_conversation_tokens,
                        "reasoning": reasoning,
                        "usage": {
                            "input": input_tokens,
                            "output": output_tokens,
                            "total_input": self.total_input_tokens,
                            "total_output": self.total_output_tokens,
                        }
                    }
                    return
                
                # Otherwise continue to next turn
            else:
                # No tool calls: this is the final answer
                # Update execution state: transition to FINALIZED
                events = self.state.set_execution_state(ExecutionState.FINALIZED)
                for event in events:
                    self._handle_state_event(event)
                
                if self.logger:
                    self.logger.log_agent_end("final_no_tools", "Final answer without tool calls", content)
                    self.logger.close()
                yield {
                    "type": "final",
                    "content": content,
                     "context_length": self.state.current_conversation_tokens,
                    "history": self.session.user_history.copy() if self.session is not None else self.conversation.copy(),
                    "reasoning": reasoning,
                    "usage": {
                        "input": input_tokens,
                        "output": output_tokens,
                        "total_input": self.total_input_tokens,
                        "total_output": self.total_output_tokens,
                    }
                }
                return
        
        # Max turns reached without final answer
        # Update execution state: transition to MAX_TURNS_REACHED
        events = self.state.set_execution_state(ExecutionState.MAX_TURNS_REACHED)
        for event in events:
            self._handle_state_event(event)
        
        if self.logger:
            self.logger.log_max_turns_reached()
            self.logger.log_agent_end("max_turns", f"Maximum turns ({self.config.max_turns}) reached without final answer")
            self.logger.close()
        yield {
            "type": "max_turns",
            "turn": self.config.max_turns,
             "context_length": self.state.current_conversation_tokens,
            "history": self.session.user_history.copy() if self.session is not None else self.conversation.copy(),
            "usage": {"input": last_input_tokens, "output": last_output_tokens,
                      "total_input": self.total_input_tokens, "total_output": self.total_output_tokens}
        }
    @classmethod
    def from_preset(cls, preset_name_or_obj, api_key: str = "", base_url: str = "https://api.deepseek.com", session: Optional['Session'] = None, **overrides):
            """
        Create an Agent instance from a preset configuration.

        Args:
            preset_name_or_obj: Either a preset name (str) to load, or a Preset object.
            api_key: API key for the LLM provider (overrides preset if provided).
            base_url: Base URL for the LLM provider (overrides preset if provided).
            session: Optional Session object to bind to the agent.
            **overrides: Additional AgentConfig fields to override preset values.

        Returns:
            Agent: Configured Agent instance.
        """
            from preset_loader import get_preset_loader
            from tools import SIMPLIFIED_TOOL_CLASSES

            # Resolve preset
            if isinstance(preset_name_or_obj, str):
                loader = get_preset_loader()
                preset = loader.get_preset(preset_name_or_obj)
                if preset is None:
                    raise ValueError(f"Preset '{preset_name_or_obj}' not found. Available: {loader.list_presets()}")
            else:
                preset = preset_name_or_obj

            # Build tool_classes from preset.tools (list of tool class names)
            tool_classes = []
            preset_tool_names = set(preset.tools or [])
            for cls in SIMPLIFIED_TOOL_CLASSES:
                if cls.__name__ in preset_tool_names:
                    tool_classes.append(cls)

            # Build AgentConfig fields from preset
            config_data = {
                "api_key": api_key or "",
                "base_url": base_url,
                "model": preset.model,
                "temperature": preset.temperature,
                "tool_classes": tool_classes,
                "enabled_tools": list(preset_tool_names),
                "system_prompt": preset.system_prompt,
                "provider_type": "openai_compatible",
                "max_turns": overrides.get("max_turns", 100),
                "detail": overrides.get("detail", "normal"),
                "workspace_path": overrides.get("workspace_path"),
                "tool_output_token_limit": overrides.get("tool_output_token_limit", 10000),
                "token_monitor_enabled": overrides.get("token_monitor_enabled", True),
                "token_monitor_warning_threshold": overrides.get("token_monitor_warning_threshold", 35000),
                "token_monitor_critical_threshold": overrides.get("token_monitor_critical_threshold", 50000),
                "turn_monitor_enabled": overrides.get("turn_monitor_enabled", True),
                "turn_monitor_warning_threshold": overrides.get("turn_monitor_warning_threshold", 0.8),
                "turn_monitor_critical_threshold": overrides.get("turn_monitor_critical_threshold", 0.95),
                "critical_countdown_turns": overrides.get("critical_countdown_turns", 5),
                "enable_logging": overrides.get("enable_logging", True),
                "log_dir": overrides.get("log_dir", "./logs"),
                "log_level": overrides.get("log_level", "INFO"),
                "enable_file_logging": overrides.get("enable_file_logging", True),
                "enable_console_logging": overrides.get("enable_console_logging", False),
                "jsonl_format": overrides.get("jsonl_format", True),
                "max_file_size_mb": overrides.get("max_file_size_mb", 10),
                "max_backup_files": overrides.get("max_backup_files", 5),
            }

            # Apply any remaining overrides (including initial_conversation, stop_check, etc.)
            config_data.update(overrides)

            # Create AgentConfig
            from agent_core import AgentConfig
            config = AgentConfig(**config_data)

            # Create Agent instance
            return cls(config, session=session)
