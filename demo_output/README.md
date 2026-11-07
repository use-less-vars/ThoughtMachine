# Demo Output for ThoughtMachine Frontend

## How to Generate the GIF

### Quick Start (if you have Node.js + ffmpeg)

```bash
# 1. Install puppeteer
npm install puppeteer

# 2. Start the Vite dev server (in another terminal)
cd web_ui/frontend
npm run dev

# 3. Run the GIF generator
node scripts/generate_demo_gif.mjs
```

The script will:
1. Open the app in a headless browser
2. Navigate through each feature
3. Take screenshots at each step
4. Stitch them into `demo_output/demo.gif`

### Manual Recording (no tools needed)

1. Start the dev server: `cd web_ui/frontend && npm run dev`
2. Open your browser's DevTools → Console
3. Use your system's screen recorder (QuickTime on Mac, Xbox Game Bar on Windows, Peek on Linux)
4. Record this flow:
   - **App loads** with one session tab
   - **Open the worker panel** — click the toggle button
   - **Create a new session tab** — click the "+" button
   - **Switch between tabs** — show worker panel state is per-tab
   - **Trigger the error boundary** — type this in console: `throw new Error("test")`
5. Convert the recording to GIF at [ezgif.com](https://ezgif.com)

### What to Demo

| Step | What to Show |
|------|-------------|
| 1 | App loading with a session tab |
| 2 | Opening the worker panel in tab 1 |
| 3 | Creating a new session tab 2 |
| 4 | Tab 2 has worker panel **closed** (per-session state) |
| 5 | Opening worker in tab 2, switching back — tab 1 is still open |
| 6 | Trigger an error → error boundary shows "Something went wrong" |
| 7 | Click "Reload" → app recovers |
