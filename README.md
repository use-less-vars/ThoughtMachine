### Current version 0.9
## How to use: 
1. clone repo, write API key into .env 
2. create a venv with "python3 -m venv .venv"
3. install dependencies "pip3 install -r requirements.txt"
4. setup docker (if you want the agent to be able to run code in a save environment):
    first install docker, then build the image:
```bash
docker build -f docker/executor.Dockerfile -t agent-executor .
```

5. now run: python3 run_gui.py and it should start. Setup your provider (base url, if you have the key in .env leave api-key field empty, else write key there directly, finally choose model)

6. Set the workspace (where it can write/read. Also this workspace is mounted into the container)



