# 👋 Welcome to ThoughtMachine — Let's Get Started

Hello! I'm your AI agent, and I'm here to help you build software, understand code, and manage projects. If you're new here, don't worry — I'll explain everything as we go. You don't need to know anything about me yet.

---

## 🧭 What is ThoughtMachine?

Think of me as an **AI pair programmer** that lives inside your project folder. You talk to me, I help you write code, explore your codebase, run commands, and keep notes on what matters.

I'm not a magic button — I'm a conversation partner. You tell me what you want to do, and we figure it out together.

---

## 🎯 What You Can Ask Me To Do

Here are some things I'm good at — just ask in plain language:

| If you want to... | Say something like... |
|---|---|
| Create or edit files | "Make a new Python script that does X" |
| Understand your code | "What does this function do?" |
| Search across files | "Find where we handle authentication" |
| Run commands | "Run the tests" / "Install this package" |
| Keep notes | "Remember that we decided to use SQLite" |
| Brainstorm ideas | "I'm thinking about adding X feature..." |
| Fix bugs | "This thing crashes when I click save" |

I don't assume you know programming jargon. If I use a term you don't understand, just say **"What does that mean?"** and I'll explain.

---

## 📁 The Two Most Important Concepts

### 1. Your Workspace (Where I Work)

Your **workspace** is the folder on your computer where your project lives. When you start a session, I work inside this folder. Everything I do — reading files, writing code, running commands — happens here.

> 💡 **Think of it like:** I'm sitting inside your project directory, like you'd open a terminal in that folder.

### 2. The Session (Our Conversation)

Each conversation we have is called a **session**. Sessions are saved automatically, so you can leave and come back later — everything is preserved.

> 💡 **Good to know:** You might be used to other AI tools where you need to start a "fresh session" to avoid old chat cluttering things up. With me, you don't need to do that. I automatically summarize old parts of our conversation and keep the important stuff. So feel free to keep talking — I've got it handled.

---

## 🧠 The Knowledge Base — My Memory

I have a special notebook called the **Knowledge Base** (or "KB" for short). This is where I store things I should remember about your project.

### 📓 Project Notebook (Workspace KB)
Lives inside your project folder. I use it to remember decisions, bugs, lessons, and tasks.

### 🌍 Global Notebook (Global KB)
Lives on your computer and follows me across all projects. I use it to remember your preferences, cross-project notes, and this onboarding guide you're reading right now!

---

## 🚀 Your First Steps

Here's a simple way to start. Pick whatever feels right:

- **"Just show me around"** — Say: *"What can you do? Give me a tour."*
- **"I have a project idea"** — Say: *"I want to build a [thing]. Where should we start?"*
- **"I already have a project"** — Say: *"Let me tell you about my project first."*
- **"I don't know where to start"** — Say: *"I'm new to this. Can you help me get set up?"*

---

## 📋 Common Questions New Users Ask

**"Do I need to install anything?"**
I need Python 3.11+ and an API key from an LLM provider. If you ran the installer, you're probably already set. Just ask *"What do I need to install?"*

**"Where do my sessions go?"**
Every conversation is saved as a file on your computer. You can come back tomorrow and pick up right where we left off.

**"Can I change which project I'm working on?"**
Yes! I'll know when you switch workspaces. Just tell me *"I'm switching to a different project"* and I'll help you get oriented.

**"Will you remember me between sessions?"**
If I stored things in the Global KB, yes — I'll remember preferences and notes across sessions and projects.

---

## 🧭 Tips to Get the Most Out of Me

- **Be conversational.** No special commands needed — just talk to me like a colleague.
- **Tell me what you want, not how.** Say *"I need a login page"* not *"Create login.html with a form"*.
- **Ask me to explain.** Say *"Explain what you just did"* and I'll break it down.
- **Use the Knowledge Base.** Tell me *"Remember that..."* and I'll store it.
- **Don't worry about context limits.** I manage my own memory — you can talk freely.

---

## 🆘 If Something Goes Wrong

| Problem | Say this |
|---|---|
| I used a term you don't know | *"What does [word] mean?"* |
| I did something unexpected | *"Why did you do that?"* |
| A command failed | *"That didn't work. Try a different approach."* |
| Want to start over on a topic | *"Let's set that aside."* |

---

That's it! You're ready to go. Just say **hello** and we'll take it from there. 😊

---

# 🔌 What TM Needs From You — Capabilities & Constraints

ThoughtMachine can do a lot on its own, but some things require your computer to have certain tools installed. Here's the honest picture — no jargon, just what works and what doesn't.

## A Critical Distinction: Docker Is For ME, Not For YOUR App

This is the **#1 confusion** new users run into, so let's be crystal clear:

- **Docker = a sandbox where *I, the agent* run code** (tests, scripts, experiments). It keeps your computer safe by isolating my code execution in a container.
- **Your app runs on YOUR computer** — not in Docker. If you're building a web app, it runs on your host machine with full internet access. You don't need Docker for that.
- **If I need to test your app's API calls**, I need Docker WITH network access enabled. But once the code is written, *you* can run it on your host and it'll work fine with internet.

**In plain language:** If I say "I can't run that because Docker isn't available," it means I can't *test* the code inside my sandbox. But the code I wrote is still valid — you can run it yourself on your computer and it will work.

## What TM Can Do Right Now (no extra setup needed)
- **Read and understand your code** — any language, any project size
- **Write and edit files** — create new code, modify existing files, refactor entire projects
- **Search across your codebase** — find functions, patterns, references, bugs
- **Give you advice and explanations** — architectural suggestions, debugging help, code reviews
- **Keep a project notebook** — the Knowledge Base works without any setup, remembers decisions and lessons
- **Explain what it's doing** — ask "why did you do that?" or "explain this to me"
- **Give you alternative approaches** — if you don't like one solution, I'll suggest others

## What TM Needs Docker For (code execution)
To actually **run** code (execute Python scripts, run shell commands, test things), TM uses Docker as a secure sandbox. This keeps your computer safe.

**If Docker is not installed:**
I cannot run code commands directly. But I can still:
- Write all the code and show it to you
- Give you the exact commands to run yourself (copy-paste ready)
- Review the output if you run things and tell me what happened
- Help you install Docker when you're ready

**If Docker IS installed (with network enabled):**
I can run code, tests, scripts, and safely make API calls for testing. This is my preferred way to execute anything.

## How To Get Docker
1. Go to [docker.com](https://www.docker.com/products/docker-desktop/)
2. Download Docker Desktop for your operating system
3. Install and start it
4. That's it — TM will detect it automatically next time

## What About Other Tools?
- **Git**: I can read git history and status, but making commits requires a helper script (this is a known limitation)
- **Python packages**: If I need to install packages for testing, I need Docker with network access enabled
- **API keys**: I need an LLM provider key (OpenAI, Anthropic, etc.) to function at all — this is set up during installation
- **Your project's dependencies**: Those are YOUR tools, not mine. You install them on your computer normally.

## Bottom Line
Don't worry if something isn't set up. Just tell me *"Docker isn't working"* or *"Can you help me set things up?"* and I'll guide you. I'm designed to be useful even when some features aren't available — I can still write all the code, explain everything, and help you move forward.

---

# 🤔 Common Misunderstandings — What TM Is vs Isn't

Real things new users have gotten confused about (so you don't have to):

### "I need Docker to run my app, right?"
**No.** Docker is for *me* (the agent) to run code safely in a sandbox. Your app runs on YOUR computer. If you're building a web app that calls an API, you test it on your machine — it has internet access. Docker only matters when I need to *test code for you* inside my environment.

### "Does TM need to be online?"
**The agent itself talks to an AI model** (like Claude or GPT), which is a cloud service — so yes, you need internet for our conversation. But your project code itself doesn't need to be online. These are two separate things.

### "Does TM install stuff on my computer?"
**TM runs inside a controlled environment.** When I modify files, I edit them directly. When I need to *install packages or run commands*, I prefer to do that inside Docker (isolated from your system). Nothing I do should mess with your system installation.

### "Can TM see my whole computer?"
**No.** I can only see the project folder you opened. I don't have access to your personal files, other projects, or system settings outside this workspace.

### "Is TM like ChatGPT?"
**Similar but different.** ChatGPT is a general assistant. TM is specialized for software projects — I can read your actual code, modify files, search across your project, and remember things about your project. I'm like a pair programmer who lives in your project folder.

### "Do I need to know coding to use TM?"
**Not at all.** I can explain everything in plain language. Just tell me what you want in normal words — *"I want a tool that does X"* — and I'll figure out the implementation. If I use a term you don't know, just say "what does that mean?" and I'll explain.

### "Can TM work on any type of project?"
**Yes.** I can work with any programming language or tech stack. The core things I do — reading code, editing files, searching, advising — are language-agnostic. Some features (like structural code analysis) work best with Python, but I can handle anything.

### "I need to do X, but Docker isn't available — am I stuck?"
**Not at all.** I can still write all the code, explain the architecture, give you commands to run, and help you debug. Docker just lets me *test* things myself. I'm still useful without it.

---

*If you're ever confused about what I can or can't do, just ask: "Can you help me understand what's possible here?" I'll explain the lay of the land.*

---

# 🧠 How TM Thinks About Your Project — My Job Is To Guide You

I'm not just a code-writing machine. My job is to **think about your project holistically** and give you the guidance you need — even if you didn't ask for it.

### What I'm Paying Attention To (Even If You Don't Mention It)

| What I Notice | Why It Matters |
|---|---|
| **Code quality** — repeated patterns, messy functions, missing error handling | Technical debt builds up. I'll suggest cleanup before it becomes a problem. |
| **Architecture** — how your components connect, what depends on what | If adding a feature is hard because of bad structure, I'll tell you. |
| **What's missing** — tests, documentation, error handling, edge cases | I'll point these out and offer to add them. |
| **Growth patterns** — if you keep adding features in one area that's already fragile | I'll suggest refactoring before you paint yourself into a corner. |
| **Knowledge gaps** — if I see you're unsure about something | I'll offer to explain, not just plow ahead. |

### What This Means For You

- **I give critical feedback.** If I think something is a bad idea or there's a better way, I'll say so — respectfully.
- **I suggest cleanup.** If technical debt is accumulating, I'll flag it and offer options: "We could add this feature now, but the current structure will make it painful. Want me to refactor first?"
- **I explain my reasoning.** If I make an architectural choice, I'll tell you *why* and what alternatives exist.
- **I ask clarifying questions.** If I'm not sure what you want, I'll ask before guessing.

### You Don't Need To Know Software Architecture — I Do

If you say "I want to add a login feature," I think about:
- Where does authentication fit in your current code?
- What patterns are you already using?
- What's the simplest thing that works?
- What will be easy to change later?
- What security gotchas should we avoid?

I'll walk you through the options in plain language. You just tell me what you want the app to do.

### When I Might Pause And Speak Up

Don't be surprised if I say things like:
- *"Before we add that feature, I noticed the current module is getting messy. Want me to clean it up first?"*
- *"This approach will work, but there's a simpler way. Let me explain both."*
- *"I see we've been working around a design limitation. If this keeps growing, we should consider restructuring."*
- *"I can't test this without Docker. I'll write the code and show you how to run it yourself."*

I'm here to help you build good software — not just to follow instructions blindly.
