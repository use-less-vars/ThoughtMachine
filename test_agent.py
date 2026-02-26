# test_controller.py
import time
import json
import os
from dotenv import load_dotenv
from agent_core import AgentConfig
from agent_controller import AgentController

load_dotenv()

def main():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    config = AgentConfig(
        api_key=api_key,
        model="deepseek-chat",
        max_turns=15,
        temperature=0.2,
        extra_system=None,
        # stop_check will be set by the controller
    )

    controller = AgentController()
    query = "Look if there are poems in the folder, if so, write in each file at the beginning: 'reasoner was here'"

    print("Starting agent...")
    controller.start(query, config)

    # Poll for events (like a GUI would)
    try:
        while controller.is_running:
            event = controller.get_event(block=False)
            if event:
                print(f"\n--- Event: {event['type']} ---")
                if "reasoning" in event and event["reasoning"]:
                    print("Reasoning:", event["reasoning"])
                if event["type"] == "turn":
                    print(f"Turn {event['turn']}")
                    for tc in event["tool_calls"]:
                        print(f"Tool: {tc['name']}")
                        print("Arguments:", json.dumps(tc["arguments"], indent=2))
                        print("Result:", tc["result"])
                    print("Token usage:", event["usage"])
                elif event["type"] == "final":
                    print("Final answer:", event["content"])
                    print("Total usage:", event["usage"])
                elif event["type"] == "stopped":
                    print("Agent stopped")
                elif event["type"] == "max_turns":
                    print("Max turns reached")
                elif event["type"] == "error":
                    print("Error:", event.get("message"))
                    if "traceback" in event:
                        print(event["traceback"])
                elif event["type"] == "thread_finished":
                    print("Thread finished.")

            # In a real GUI, you would sleep a short time (or use after() in Tkinter)
            time.sleep(0.1)

            # Optional: simulate pause/resume/stop after some time (uncomment to test)
            # if time.time() - start_time > 2:
            #     controller.pause()
            #     print("Paused")
            #     time.sleep(3)
            #     controller.resume()
            #     print("Resumed")
            #     # then maybe stop later
            #     # controller.stop()

    except KeyboardInterrupt:
        print("\nStopping agent...")
        controller.stop()

    print("Done.")

if __name__ == "__main__":
    main()