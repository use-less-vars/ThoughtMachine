# agent_controller.py
import threading
import queue
import traceback
from agent_core import AgentConfig
from agent import Agent
from typing import Optional, List, Dict, Any

class AgentController:
    """
    Runs the agent in a background thread and provides thread‑safe control
    via start/stop/pause/resume and a queue for receiving events.
    """

    def __init__(self):
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
        self.agent = None
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

        print("[Controller] Reset to initial state")

    @property
    def is_running(self):
        """Return True if the agent thread is alive."""
        # Check both the running flag and thread status
        if self.thread is not None and self.thread.is_alive():
            return True
        # Thread is dead or doesn't exist, ensure state is cleaned up
        if self._running:
            # Thread died unexpectedly, clean up
            self._cleanup_if_thread_dead()
        return self._running
    
    def get_config(self):
        """Return the current AgentConfig being used."""
        return self._config

    def start(self, query: str, config: AgentConfig, initial_conversation: Optional[List[Dict[str, Any]]] = None):
        """
        Start the agent with the given query and configuration.

        Args:
            query: The user query string.
            config: An AgentConfig instance (api_key, model, etc.).
            initial_conversation: Optional previous conversation history to continue from.
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

        # Store query and config for the background thread
        self._query = query
        self._config = config
        self._initial_conversation = initial_conversation
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
            raise RuntimeError("Agent not running. Use start() first.")
        self.resume()
        self.query_queue.put(query)

    def request_pause(self):
        """Request agent to pause after current turn."""
        if not self.is_running:
            raise RuntimeError("Agent not running. Use start() first.")
        if self._processing_query:
            # Agent is currently processing a query, set pause flag
            self.pause()
        else:
            # Agent is idle, send paused event directly
            self.event_queue.put({"type": "paused"})
    def restart_session(self):
        """Restart agent with cleared history."""
        if not self.is_running:
            raise RuntimeError("Agent not running. Use start() first.")
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

    def get_event(self, block=False, timeout=None):
        """
        Retrieve an event from the queue.

        Args:
            block: If True, wait until an event is available.
            timeout: Maximum time to wait when block=True (None = wait forever).

        Returns:
            The next event dict, or None if no event is available (when block=False).
        """
        try:
            return self.event_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

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

            # Inject the stop_check into a copy of the config to avoid mutating the original
            run_config = self._config.model_copy() if hasattr(self._config, 'model_copy') else self._config
            run_config.stop_check = should_stop
            if self._initial_conversation is not None:
                run_config.initial_conversation = self._initial_conversation

            # Create Agent instance
            agent = Agent(run_config, initial_conversation=self._initial_conversation)
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
                    self.event_queue.put({"type": "paused"})
                    continue

                print(f"[Controller] Processing query: {query[:50]}...")
                self._processing_query = True
                # Run the agent for this query
                for event in agent.process_query(query):
                    print(f"[Controller] Event: {event['type']}")
                    # Put each event into the queue for the GUI to pick up
                    self.event_queue.put(event)

                    # If this is a terminal event, decide what to do
                    if event["type"] in ("stopped", "error", "max_turns"):
                        # These are fatal, stop the whole agent thread
                        self._keep_alive = False
                        break
                    elif event["type"] in ("final", "user_interaction_requested"):
                        # Agent has completed this query, pause and wait for next query
                        # Yield a paused event to inform GUI
                        print("[Controller] Sending paused event")
                        self.event_queue.put({"type": "paused"})
                        break
                    # For other events (turn), continue processing
                    # Check if pause requested after a turn
                    if event["type"] == "turn" and self._pause_requested:
                        print("[Controller] Pause requested, breaking after turn")
                        self._pause_requested = False
                        self.event_queue.put({"type": "paused"})
                        break

                self._processing_query = False
                # If _keep_alive becomes False, break outer loop
                if not self._keep_alive:
                    break

        except Exception as e:
            # Catch any unexpected exception and send an error event
            print(f"[Controller] Exception in _run: {e}")
            traceback.print_exc()
            self.event_queue.put({
                "type": "error",
                "message": str(e),
                "traceback": traceback.format_exc()   # helpful for debugging
            })
        finally:
            # Signal that the thread is finishing
            self.event_queue.put({"type": "thread_finished"})
            self._running = False