# test_agent.py
import json
from agent_core import run_agent_stream, AgentConfig
from dotenv import load_dotenv
import os       

load_dotenv()  # Load environment variables from .env file  

def main():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    api_key = api_key   # replace with actual

    config = AgentConfig(
        api_key=api_key,
        model="deepseek-chat",       # or "deepseek-chat"
        max_turns=15,
        temperature=0.2,
        extra_system=None,
        stop_check=lambda: False
    )

    query = "Look if there are poems in the folder, if so, write in each file at the beginning: 'reasoner was here'"

    print("Starting agent...")
    for event in run_agent_stream(query, config):
        print(f"\n--- Event: {event['type']} ---")
        if "reasoning" in event and event["reasoning"]:
            print("Reasoning:", event["reasoning"])
        if event["type"] == "turn":
            print(f"Turn {event['turn']}")
            # tool_calls is a list
            for tc in event["tool_calls"]:
                print(f"Tool: {tc['name']}")
                print("Arguments:", json.dumps(tc["arguments"], indent=2))
                print("Result:", tc["result"])
            print("Token usage:", event["usage"])
        elif event["type"] == "final":
            print("Final answer:", event["content"])
            print("Total usage:", event["usage"])
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