# Capabilities Reference

Structured reference mapping what TM can/can't do, what requires what, for explaining to users in plain language.

---

## 🟢 Always Available (No Setup Needed)

These work in any project, any language, any setup:

| Capability | What It Means | How To Explain To User |
|---|---|---|
| **Read & understand code** | Parse any file, explain what it does, find patterns | "I can read your entire project and understand what every file does. Just ask me 'what does this function do?'" |
| **Write & edit files** | Create new files, modify existing ones, refactor | "I can write code and save it directly to your project. Tell me what you want and I'll create it." |
| **Search codebase** | Find functions, patterns, references, text across all files | "Need to find where something is defined? I can search across your whole project in seconds." |
| **Knowledge Base** | Remember decisions, track bugs, keep notes | "I have a notebook that remembers things between conversations. Just tell me 'remember that...'" |
| **Explain & advise** | Architectural guidance, code review, options analysis | "I can explain what your code does, suggest improvements, and help you make decisions." |
| **Answer questions** | About your project, about software concepts, about what I'm doing | "If I use a term you don't know, just say 'what does that mean?' and I'll explain in plain language." |

## 🟡 Available With Docker

These need Docker installed and running:

| Capability | Requirement | How To Explain To User |
|---|---|---|
| **Run code in sandbox** | Docker | "I can run your code inside a safe, isolated container to test it. This keeps your computer safe." |
| **Run shell commands** | Docker | "I can run terminal commands — install packages, run scripts, check outputs." |
| **Test API calls** | Docker + network enabled | "If your app makes API calls, I can test them inside the container. But remember: your app on your host already has internet." |
| **Install packages for testing** | Docker + network enabled | "I can pip install things inside the container to test with." |

## 🔴 Not Available (And Likely Never Will Be)

| Not Available | Why | What To Offer Instead |
|---|---|---|
| **Direct network access from agent** | Security — agent runs in isolation | Use Docker with network, or tell user to test on their host |
| **Access files outside workspace** | Privacy by design | Ask user to copy relevant files into workspace |
| **Modify system settings** | Safety — agent shouldn't touch system config | Guide user verbally on what to change |
| **Execute arbitrary code on host** | Security isolation | Use Docker sandbox, or give user commands to run themselves |
| **Send emails, make real API calls** | Not the agent's role | Write the code for the user to deploy and run |

## 🔑 Key Distinctions To Always Make Clear

### Docker: For ME, Not For YOUR App

This is the #1 confusion. When I say "I need Docker," I mean:
- *I* need a sandbox to *test* code safely
- Your app runs on YOUR computer, with YOUR internet
- If Docker is missing: I can still write code, I just can't test it myself
- Your app's functionality (API calls, network requests) works fine on your host

### Code Execution vs Code Writing

| I can write code | ✅ Always |
|---|---|
| I can explain code | ✅ Always |
| I can search code | ✅ Always |
| I can run code to test it | ✅ With Docker |
| I can commit to git | ⚠️ Partial (can read git, needs helper for commits) |

## 🗺️ The Landscape Map — What Different Setups Enable

Use this when a user asks "what's different if I set up X?"

### Minimal Setup (just Python + API key)
- ✅ Read, write, search, explain
- ✅ Knowledge Base (memory)
- ✅ Architecture advice, code review
- ❌ Can't run code
- ❌ Can't test things
- ❌ Can't install packages

### With Docker
- ✅ Everything above
- ✅ Run Python scripts, tests
- ✅ Run commands
- ✅ Install packages in container
- ⚠️ Internet only if network enabled

### With Docker + Network
- ✅ Everything above
- ✅ Test API calls from your code
- ✅ Download packages from PyPI etc
- ✅ Full development environment

## 🧠 How To Explain This To Non-Technical Users

Don't use jargon. Don't say "sandboxed container runtime." Say:
- **"I need Docker to test code safely"** — not "Docker provides OS-level virtualization"
- **"Your app runs on your computer with normal internet"** — not "the host environment has unfettered network access"
- **"I can still write all the code, just can't run it to check"** — not "code execution capability is unavailable"

If a user seems confused, offer: *"Want me to explain what's happening in plain language?"*

## 🆘 Agent Instructions

When a user asks about capabilities or something fails:

1. **First**: Explain in plain language what happened and why
2. **Second**: Offer alternatives (write code anyway, give commands to run, etc.)
3. **Third**: Ask if they want to set up the missing dependency
4. **Remember**: Most users don't know what Docker is or why it matters — be patient

Example: *"I can't test the API call right now because Docker isn't set up. But I've written the code — you can run it on your computer and it'll work fine. Want me to explain how to run it, or would you like help installing Docker?"*
