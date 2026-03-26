# agent_controller.py
import threading
import queue
import traceback
from agent_core import AgentConfig
from agent import Agent
from typing import Optional, List, Dict, Any
from PyQt6.QtCore import QObject, pyqtSignal

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
        if self.thread is not None and not self.thread.is_alive():
            # Thread has finished but state wasn't cleaned up
            self._running = False
            self.thread = None
            self.agent = None  # Clear old agent reference
            self._keep_alive = True
            self._pause_requested = False
            self._processing_query = False
            print(f"[Controller] Cleaned up dead thread, _running={self._running}")
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

        print("[Controller] Reset to initial state")

    @property
    def is_running(self):
        """Return True if the agent thread is alive and not shutting down."""
        # If _running is False, agent is definitely not running
        if not self._running:
            return False
        # Check thread status
        if self.thread is not None and self.thread.is_alive():
            return True
        # Thread is dead or doesn't exist, ensure state is cleaned up
        self._cleanup_if_thread_dead()
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
        print(f"[Controller] start called with query: {query[:50]}...")
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
        """Request the agent to stop after the current turn/tool."""
        self.stop_event.set()
        self.pause_event.set()   # if paused, resume so stop can be noticed

    def continue_session(self, query: str):
        """Submit a new query to the already running agent."""
        if not self.is_running:
            # Agent is not running, cannot continue
            return
        self.resume()
        self.query_queue.put(query)

    def request_pause(self):
        """Request agent to pause after current turn."""
        if not self.is_running:
            # Agent is not running, nothing to pause
            return
        if self._processing_query:
            # Agent is currently processing a query, set pause flag
            self.pause()
        else:
            # Agent is idle, send paused event directly
            self._emit_event({"type": "paused"})

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
        self.pause_event.clear()
        self._pause_requested = True

    def resume(self):
        """Resume a paused agent."""
        self.pause_event.set()
        self._pause_requested = False



    def _emit_event(self, event):
        """Emit event both to queue and signal."""
        # Attach session ID for event filtering
        event['session_id'] = self.current_session_id
        # Put into queue for compatibility
        self.event_queue.put(event)
        # Emit signal for presenter
        print(f"[Controller] Emitting event_occurred: {event.get('type')}")
        self.event_occurred.emit(event)

    def _run(self):
        """Internal method that runs in the background thread."""
        print("[Controller] _run started")
        try:
            # Define the stop_check function that the agent will call before each turn
            def should_stop():
                # If paused, wait until pause_event is set again
                print(f"[Controller] should_stop called, pause_event.is_set={self.pause_event.is_set()}, stop_event.is_set={self.stop_event.is_set()}")
                self.pause_event.wait()   # blocks while paused
                return self.stop_event.is_set()

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
                # Wait for next query
                try:
                    query = self.query_queue.get(timeout=1.0)
                except queue.Empty:
                    # Check if we should stop
                    if self.stop_event.is_set():
                        break
                    continue

                if query == "[RESET]":
                    agent.reset()
                    continue
                if query == "[PAUSE]":
                    print("[Controller] Pause requested")
                    self._emit_event({"type": "paused"})
                    continue

                print(f"[Controller] Processing query: {query[:50]}...")
                self._processing_query = True
                # Run the agent for this query
                for event in agent.process_query(query):
                    print(f"[Controller] Event: {event['type']}")
                    # Put each event into the queue for the GUI to pick up
                    self._emit_event(event)

                    # If this is a terminal event, decide what to do
                    if event["type"] in ("stopped", "error", "max_turns"):
                        # These are fatal, stop the whole agent thread
                        print(f"[Controller] Terminal event {event['type']} detected, setting _keep_alive=False")
                        self._keep_alive = False
                        self._running = False  # Mark as not running immediately
                        break
                    elif event["type"] in ("final", "user_interaction_requested"):
                        # Agent has completed this query, pause and wait for next query
                        # Yield a paused event to inform GUI
                        print("[Controller] Sending paused event")
                        self._emit_event({"type": "paused"})
                        break
                    # For other events (turn), continue processing
                    # Check if pause requested after a turn
                    if event["type"] == "turn" and self._pause_requested:
                        print("[Controller] Pause requested, breaking after turn")
                        self._pause_requested = False
                        self._emit_event({"type": "paused"})
                        break

                self._processing_query = False
                # If _keep_alive becomes False, break outer loop
                if not self._keep_alive:
                    print(f"[Controller] _keep_alive=False, breaking outer loop")
                    break

        except Exception as e:
            # Catch any unexpected exception and send an error event
            print(f"[Controller] Exception in _run: {e}")
            traceback.print_exc()
            self._running = False  # Mark as not running before sending error
            self._emit_event({
                "type": "error",
                "message": str(e),
                "traceback": traceback.format_exc()   # helpful for debugging
            })
        finally:
            print("[Controller] Finally block: thread finishing")
            # Signal that the thread is finishing
            self._emit_event({"type": "thread_finished"})
            self._running = False