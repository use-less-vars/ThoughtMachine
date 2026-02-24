# test_agent.py
import json
from agent_core import run_agent_stream, AgentConfig
from dotenv import load_dotenv
import os

def main():
    # Replace with your actual API key or load from file
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    config = AgentConfig(
        api_key=api_key,
        max_turns=5,
        temperature=0.2,
        extra_system=None,
        stop_check=lambda: False   # no stop
    )

    query = "What is 15 * 27? Then write the result to result.txt"

    print("Starting agent...")
    for event in run_agent_stream(query, config):
        print(f"\n--- Event: {event['type']} ---")
        if event["type"] == "turn":
            print(f"Turn {event['turn']}")
            print("Tool call:", json.dumps(event["tool_call"], indent=2))
            print("Result:", event["tool_result"])
            print("Token usage:", event.get("usage"))
        elif event["type"] == "final":
            print("Final answer:", event["content"])
            print("Total usage:", event.get("usage"))
            break
        elif event["type"] == "stopped":
            print("Agent stopped")
            break
        elif event["type"] == "max_turns":
            print("Max turns reached")
            break
        elif event["type"] == "error":
            print("Error:", event.get("message"))
            break

if __name__ == "__main__":
    main()