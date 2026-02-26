# agent_controller.py
import threading
import queue
import traceback
from agent_core import run_agent_stream, AgentConfig

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

    @property
    def is_running(self):
        """Return True if the agent thread is alive."""
        return self._running

    def start(self, query: str, config: AgentConfig):
        """
        Start the agent with the given query and configuration.

        Args:
            query: The user query string.
            config: An AgentConfig instance (api_key, model, etc.).
        """
        if self._running:
            raise RuntimeError("Agent is already running. Stop it first.")

        # Reset control events
        self.stop_event.clear()
        self.pause_event.set()   # ensure we start unpaused

        # Store query and config for the background thread
        self._query = query
        self._config = config

        # Create and start the daemon thread
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._running = True
        self.thread.start()

    def stop(self):
        """Request the agent to stop after the current turn/tool."""
        self.stop_event.set()
        self.pause_event.set()   # if paused, resume so stop can be noticed

    def pause(self):
        """Pause the agent before the next turn (finishes current turn first)."""
        self.pause_event.clear()

    def resume(self):
        """Resume a paused agent."""
        self.pause_event.set()

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
        try:
            # Define the stop_check function that the agent will call before each turn
            def should_stop():
                # If paused, wait until pause_event is set again
                self.pause_event.wait()   # blocks while paused
                return self.stop_event.is_set()

            # Inject the stop_check into a copy of the config to avoid mutating the original
            # (optional, but good practice)
            run_config = self._config.model_copy() if hasattr(self._config, 'model_copy') else self._config
            run_config.stop_check = should_stop

            # Run the agent stream
            for event in run_agent_stream(self._query, run_config):
                # Put each event into the queue for the GUI to pick up
                self.event_queue.put(event)

                # If this is a terminal event, stop the loop (agent already stopped)
                if event["type"] in ("final", "stopped", "max_turns", "error"):
                    break

        except Exception as e:
            # Catch any unexpected exception and send an error event
            self.event_queue.put({
                "type": "error",
                "message": str(e),
                "traceback": traceback.format_exc()   # helpful for debugging
            })
        finally:
            # Signal that the thread is finishing
            self.event_queue.put({"type": "thread_finished"})
            self._running = False