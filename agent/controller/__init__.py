# agent_controller.py
import threading
import os
import queue
import traceback
from agent.config import AgentConfig
from agent import Agent
from typing import Optional, List, Dict, Any
from PyQt6.QtCore import QObject, pyqtSignal
from agent.logging.debug_log import debug_log

class AgentController(QObject):
    """
    Runs the agent in a background thread and provides thread‑safe control
    via start/stop/pause/resume and a queue for receiving events.
    """
    # Signals
    event_occurred = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        # Thread‑safe queue for passing events from agent thread to main thread
        self.event_queue = queue.Queue()

        # Events for controlling the agent thread
        self.stop_event = threading.Event()   # when set, agent should stop
        self.pause_event = threading.Event()  # when set, agent can run (otherwise paused)
        self.pause_event.set()                 # start unpaused

        # Thread handle and running flag
        self.thread = None
        self._running = False
        self._initial_conversation = None
        self._agent_override = None
        self.agent = None
        self.current_session_id = None  # For event filtering
        # Query queue for keep-alive mode
        self.query_queue = queue.Queue()
        self._keep_alive = True
        self._pause_requested = False
        self._processing_query = False

    def _cleanup_if_thread_dead(self):
        """Check if background thread is dead and reset state if needed."""
        debug_log(f"_cleanup_if_thread_dead called, thread={'alive' if self.thread and self.thread.is_alive() else 'dead/None'}", level="DEBUG", component="Controller")
        if self.thread is not None and not self.thread.is_alive():
            # Thread has finished but state wasn't cleaned up
            debug_log(f"Thread dead, cleaning up state", level="DEBUG", component="Controller")
            self._running = False
            self.thread = None
            self.agent = None  # Clear old agent reference
            self._keep_alive = True
            self._pause_requested = False
            self._processing_query = False
            debug_log(f"Cleaned up dead thread, _running={self._running}", level="DEBUG", component="Controller")
    def reset(self):
        """Reset controller to initial state, clearing all queues and events."""
        # Clear event queue
        while True:
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                break

        # Clear query queue  
        while True:
            try:
                self.query_queue.get_nowait()
            except queue.Empty:
                break

        # Reset events
        self.stop_event.clear()
        self.pause_event.set()  # start unpaused

        # Reset state
        self.thread = None
        self._running = False
        self._initial_conversation = None
        self.agent = None
        self._keep_alive = True
        self._pause_requested = False
        self._processing_query = False
        self.current_session_id = None

        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            debug_log(f"Reset to initial state", level="DEBUG", component="Controller")

    @property
    def is_running(self):
        """Return True if the agent thread is alive and not shutting down."""
        # If _running is False, agent is definitely not running
        if not self._running:
            debug_log(f"is_running: _running=False, returning False (thread alive={self.thread.is_alive() if self.thread else False})", level="DEBUG", component="Controller")
            return False
        # Check thread status
        if self.thread is not None and self.thread.is_alive():
            debug_log(f"is_running: thread alive, returning True (_running={self._running}, pause_event.is_set={self.pause_event.is_set()}, _pause_requested={self._pause_requested})", level="DEBUG", component="Controller")
            return True
        # Thread is dead or doesn't exist, ensure state is cleaned up
        debug_log(f"is_running: thread dead or None, cleaning up (thread={self.thread})", level="DEBUG", component="Controller")
        self._cleanup_if_thread_dead()
        debug_log(f"is_running: after cleanup, _running={self._running}", level="DEBUG", component="Controller")
        return self._running
    
    def get_config(self):
        """Return the current AgentConfig being used."""
        return self._config

    def start(self, query: str, config: AgentConfig = None, session=None, preset_name: str = None, **overrides):
        """
        Start the agent with the given query and configuration.

        Args:
            query: The user query string.
            config: An AgentConfig instance (api_key, model, etc.). Mutually exclusive with preset_name.
            session: Optional Session instance to associate with this run (for history persistence).
            preset_name: Name of a preset to use instead of config. If provided, config is ignored.
            **overrides: Additional config overrides when using preset_name.
        """
        debug_log(f"start called with query: {query[:50]}...", level="DEBUG", component="Controller")
        # Clean up any dead thread state
        self._cleanup_if_thread_dead()
        if self._running:
            raise RuntimeError("Agent is already running. Stop it first.")

        # Reset control events
        self.stop_event.clear()
        self.pause_event.set()   # ensure we start unpaused
        # Reset internal state flags
        self._keep_alive = True
        self._pause_requested = False
        self._processing_query = False

        # Resolve configuration: if preset_name provided, use Agent.from_preset
        if preset_name is not None:
            if config is not None:
                raise ValueError("Cannot specify both config and preset_name")
            from agent import Agent
            agent = Agent.from_preset(preset_name, session=session, **overrides)
            # Extract the config from the created agent
            resolved_config = agent.config
            # Store the agent directly (no need to create a new one in _run)
            self._agent_override = agent
        else:
            if config is None:
                raise ValueError("Must provide either config or preset_name")
            resolved_config = config
            self._agent_override = None

        # Store query and config for the background thread
        self._query = query
        self._config = resolved_config
        self._session = session
        # Set session ID for event filtering (use session.session_id if available)
        self.current_session_id = session.session_id if session is not None else None
        # Enqueue the initial query
        self.query_queue.put(query)

        # Create and start the daemon thread
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._running = True
        self.thread.start()

    def stop(self):
        """Request the agent to pause after the current turn/tool."""
        self.pause()   # treat stop as pause

    def continue_session(self, query: str):
        """Submit a new query to the already running agent."""
        debug_log(f"continue_session called: query='{query[:50]}...' is_running={self.is_running} pause_event.is_set={self.pause_event.is_set()}", level="DEBUG", component="Controller")
        if os.environ.get('PAUSE_DEBUG'):
            debug_log(f"Controller.continue_session: query='{query[:50]}...', is_running={self.is_running}, pause_event.is_set={self.pause_event.is_set()}, _pause_requested={self._pause_requested}", level="WARNING", component="PAUSE_FLOW")
        if not self.is_running:
            # Agent is not running, cannot continue
            debug_msg = f"[Controller] Agent not running, cannot continue. _running={self._running}, thread alive={self.thread.is_alive() if self.thread else False}"
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                debug_log(debug_msg, level="DEBUG", component="Controller")
            if os.environ.get('PAUSE_DEBUG'):
                debug_log(f"Controller.continue_session: {debug_msg}", level="WARNING", component="PAUSE_FLOW")
            raise RuntimeError(f"Agent controller not running: {debug_msg}")
        if os.environ.get('PAUSE_DEBUG'):
            debug_log(f"Controller.continue_session: calling resume() and queuing query", level="WARNING", component="PAUSE_FLOW")
        self.resume()
        self.query_queue.put(query)
        debug_log(f"Query queued, queue size approx {self.query_queue.qsize()}", level="DEBUG", component="Controller")
        if os.environ.get('PAUSE_DEBUG'):
            debug_log(f"Controller.continue_session: query queued, queue size={self.query_queue.qsize()}", level="WARNING", component="PAUSE_FLOW")

    def request_pause(self):
        """Request agent to pause after current turn."""
        debug_log(f"request_pause called: is_running={self.is_running} _processing_query={self._processing_query} pause_event.is_set={self.pause_event.is_set()}", level="DEBUG", component="Controller")
        if not self.is_running:
            # Agent is not running, nothing to pause
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                debug_log(f"Agent not running, nothing to pause", level="DEBUG", component="Controller")
            return
        if self._processing_query:
            # Agent is currently processing a query, set pause flag
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                debug_log(f"Agent processing query, calling pause()", level="DEBUG", component="Controller")
            self.pause()
        else:
            # Agent is idle, send paused event directly
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                debug_log(f"Agent idle, sending paused event directly", level="DEBUG", component="Controller")
            # Clear pause_event to prevent processing and mark as paused
            self.pause_event.clear()
            self._pause_requested = True
            # Signal agent to pause after current turn
            if hasattr(self, 'agent') and self.agent is not None and hasattr(self.agent, 'request_pause'):
                self.agent.request_pause()
            self._emit_event({"type": "paused"})
            # Clean up orphaned tool sequences
            if hasattr(self, 'agent') and self.agent is not None:
                from session.context_builder import ContextBuilder
                if hasattr(self.agent, 'conversation'):
                    original_len = len(self.agent.conversation)
                    self.agent.conversation = ContextBuilder._cleanup_orphaned_tool_messages(self.agent.conversation)
                    if original_len != len(self.agent.conversation) and os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                        debug_log(f"Cleaned {original_len - len(self.agent.conversation)} orphaned tool messages on idle pause", level="WARNING", component="Controller")

    def get_conversation(self) -> Optional[List[Dict[str, Any]]]:
        """Return the current conversation from the agent, if available."""
        if self.agent:
            # If using a session, the conversation is the session's user_history
            return self.agent.conversation.copy() if self.agent.conversation is not None else None
        return None

    def update_runtime_params(self, **kwargs):
        """Forward runtime parameter updates to the agent if available."""
        if self.agent is not None:
            self.agent.update_runtime_params(**kwargs)

    def restart_session(self):
        """Restart agent with cleared history."""
        if not self.is_running:
            # Agent is not running, cannot restart
            return
        if self.agent:
            self.agent.request_reset()
        # Also submit a sentinel to trigger reset in queue
        self.query_queue.put("[RESET]")

    def pause(self):
        """Pause the agent before the next turn (finishes current turn first)."""
        debug_log(f"pause() called, clearing pause_event, setting _pause_requested=True", level="DEBUG", component="Controller")
        self.pause_event.clear()
        self._pause_requested = True
        # Signal agent to pause after current turn
        if hasattr(self, 'agent') and self.agent is not None and hasattr(self.agent, 'request_pause'):
            self.agent.request_pause()
        # Clean up any orphaned tool sequences in the agent
        if hasattr(self, 'agent') and self.agent is not None:
            # Import the cleanup method
            from session.context_builder import ContextBuilder
            # Clean up conversation in agent
            if hasattr(self.agent, 'conversation'):
                original_len = len(self.agent.conversation)
                self.agent.conversation = ContextBuilder._cleanup_orphaned_tool_messages(self.agent.conversation)
                if original_len != len(self.agent.conversation) and os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                    debug_log(f"Cleaned {original_len - len(self.agent.conversation)} orphaned tool messages on pause", level="WARNING", component="Controller")

    def resume(self):
        """Resume a paused agent."""
        debug_log(f"resume() called, setting pause_event, clearing _pause_requested", level="DEBUG", component="Controller")
        if os.environ.get('PAUSE_DEBUG'):
            debug_log(f"Controller.resume: setting pause_event, clearing _pause_requested", level="WARNING", component="PAUSE_FLOW")
        self.pause_event.set()
        self._pause_requested = False
        # Also clear pause request flag in agent if it exists
        if hasattr(self, 'agent') and self.agent is not None:
            if hasattr(self.agent, '_pause_requested'):
                debug_log(f"Clearing agent._pause_requested (was {self.agent._pause_requested})", level="DEBUG", component="Controller")
                if os.environ.get('PAUSE_DEBUG'):
                    debug_log(f"Controller.resume: clearing agent._pause_requested", level="WARNING", component="PAUSE_FLOW")
                self.agent._pause_requested = False



    def _emit_event(self, event):
        """Emit event both to queue and signal."""
        # Attach session ID for event filtering
        event['session_id'] = self.current_session_id
        # Put into queue for compatibility
        self.event_queue.put(event)
        # Emit signal for presenter
        debug_log(f"Emitting event_occurred: {event.get('type')}", level="DEBUG", component="Controller")
        self.event_occurred.emit(event)

    def _run(self):
        """Internal method that runs in the background thread."""
        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
            debug_log(f"_run started", level="DEBUG", component="Controller")
        try:
            # Define the stop_check function that the agent will call before each turn
            def should_stop():
                # Check if we should stop
                debug_log(f"should_stop called, pause_event.is_set={self.pause_event.is_set()}, stop_event.is_set={self.stop_event.is_set()}, _pause_requested={self._pause_requested}", level="DEBUG", component="Controller")
                # If stop event is set, return True immediately
                if self.stop_event.is_set():
                    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                        debug_log(f"should_stop: stop_event is set, returning True", level="DEBUG", component="Controller")
                    return True
                # If paused (pause_event cleared), return "PAUSED" instead of blocking
                if not self.pause_event.is_set():
                    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                        debug_log(f"should_stop: pause_event not set, returning PAUSED", level="DEBUG", component="Controller")
                    return "PAUSED"
                # Not paused and not stopped
                if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                    debug_log(f"should_stop: not paused, returning False", level="DEBUG", component="Controller")
                return False

            # If an agent was pre-created (from preset), use it directly
            if hasattr(self, '_agent_override') and self._agent_override is not None:
                agent = self._agent_override
                # Inject stop_check into the agent's config
                agent.config.stop_check = should_stop
                # If we have a session, ensure agent is linked to it
                if hasattr(self, '_session') and self._session is not None:
                    agent.session = self._session
                    agent.conversation = self._session.user_history
                    if len(self._session.user_history) > 0:
                        from session.models import SessionState
                        events = agent.state.set_session_state(SessionState.CONTINUING)
                        for event in events:
                            agent._handle_state_event(event)
                self.agent = agent
            else:
                # Inject the stop_check into a copy of the config to avoid mutating the original
                run_config = self._config.model_copy() if hasattr(self._config, 'model_copy') else self._config
                run_config.stop_check = should_stop
                # Create Agent instance with session if available
                agent = Agent(run_config, session=self._session if hasattr(self, '_session') else None)
                self.agent = agent  # store for potential reuse

            # Main loop: process queries from queue
            while self._keep_alive:
                # Check if we should stop (paused or stopped)
                stop_result = should_stop()
                if stop_result:
                    if stop_result == "PAUSED":
                        # Paused, block here until resumed
                        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                            debug_log(f"PAUSED returned, waiting on pause_event", level="DEBUG", component="Controller")
                        self.pause_event.wait()
                        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                            debug_log(f"Resumed from pause_event.wait()", level="DEBUG", component="Controller")
                        continue
                    # Otherwise stopped (True)
                    # Agent is stopped, continue loop
                    continue
                
                # Wait for next query (only if not paused)
                debug_log(f"Before query_queue.get, queue size: {self.query_queue.qsize()}", level="DEBUG", component="Controller")
                try:
                    query = self.query_queue.get(timeout=1.0)
                    debug_log(f"Got query from queue: '{query[:50]}...'", level="DEBUG", component="Controller")
                except queue.Empty:
                    # Queue empty, continue loop
                    debug_log(f"Queue empty after timeout", level="DEBUG", component="Controller")
                    continue

                if query == "[RESET]":
                    agent.reset()
                    continue
                if query == "[PAUSE]":
                    if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                        debug_log(f"Pause requested", level="DEBUG", component="Controller")
                    self._emit_event({"type": "paused"})
                    continue

                debug_log(f"Processing query: {query[:50]}...", level="DEBUG", component="Controller")
                self._processing_query = True
                # Run the agent for this query
                for event in agent.process_query(query):
                    debug_log(f"Event: {event['type']}", level="DEBUG", component="Controller")
                    # Put each event into the queue for the GUI to pick up
                    self._emit_event(event)
                    if event["type"] == "paused":
                        self._pause_requested = False
                        # Agent has paused, break out of event loop to allow new queries
                        break

                    # If this is a terminal event, decide what to do
                    if event["type"] in ("stopped", "error", "max_turns"):
                        # Treat as pause, keep thread alive
                        debug_log(f"Terminal event {event['type']} detected, treating as pause", level="DEBUG", component="Controller")
                        # Clear pause request flag
                        self._pause_requested = False
                        # Send paused event to inform GUI
                        self._emit_event({"type": "paused"})
                        break
                    elif event["type"] in ("final", "user_interaction_requested"):
                        # Agent has completed this query, pause and wait for next query
                        # Yield a paused event to inform GUI
                        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                            debug_log(f"Sending paused event", level="DEBUG", component="Controller")
                        self._emit_event({"type": "paused"})
                        break
                    # For other events (turn), continue processing
                    # Check if we're paused between events
                    if not self.pause_event.is_set():
                        # We're paused, break out of loop
                        if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                            debug_log(f"pause_event not set between events, breaking loop", level="DEBUG", component="Controller")
                        self._pause_requested = False
                        self._emit_event({"type": "paused"})
                        break

                self._processing_query = False
                # If _keep_alive becomes False, break outer loop
                if not self._keep_alive:
                    debug_log(f"_keep_alive=False, breaking outer loop", level="DEBUG", component="Controller")
                    break

        except Exception as e:
            # Catch any unexpected exception and send an error event
            debug_log(f"Exception in _run: {e}", level="ERROR", component="Controller")
            traceback.print_exc()
            self._running = False  # Mark as not running before sending error
            self._emit_event({
                "type": "error",
                "error_type": "CONTROLLER_ERROR",
                "message": str(e),
                "traceback": traceback.format_exc()   # helpful for debugging
            })
        finally:
            if os.environ.get('THOUGHTMACHINE_DEBUG') == '1':
                debug_log(f"Finally block: thread finishing", level="DEBUG", component="Controller")
            # Signal that the thread is finishing
            self._emit_event({"type": "thread_finished"})
            self._running = False