# Configuration Guide: Gemini Proxy & Google STT Server for Third-Party Apps

This guide teaches you how to configure and use your local FastAPI proxy server as a custom Artificial Intelligence provider in third-party applications that accept endpoints compatible with OpenAI and Google Speech-to-Text.

---

## Prerequisites

1. **Python installed on Windows**: Make sure Python 3.9+ is installed and added to the Windows PATH.
2. **Client App (e.g., your third-party AI client)**: Installed and ready to accept custom API URLs.

---

## Step 1: Obtain Google Gemini Cookies

Since the local wrapper (`gemini_webapi`) emulates the browser, we need the cookies from your active Google Gemini session to authenticate.

### Method A: Sync via Automated Browser (Recommended and Auto-renewable)
This method uses Playwright to open a persistent Chromium profile under the `profiles/` folder. Once logged in, the proxy server can refresh and rotate its own short-lived cookies in the background whenever necessary.

1. Ensure the dependencies are installed in your virtual environment:
   ```bash
   pip install playwright
   playwright install chromium
   ```
2. Start the visual login flow specifying your account:
   ```bash
   # For the main account (saved in profiles/cookies.json):
   python sync_cookies.py cookies --login

   # For other accounts (e.g. profiles/cookies2.json):
   python sync_cookies.py cookies2 --login
   ```
3. The Chromium browser will open. Log in to Google if necessary, and as soon as Gemini Web fully loads, **just close the browser window**. The script will automatically save the cookies to the corresponding file!

### Method B: Export as JSON (Static Alternative)
1. Install a cookie extension (e.g. **EditThisCookie**).
2. Access [Google Gemini](https://gemini.google.com/) logged in.
3. Export cookies in **JSON** format and save them as `cookies.json` (or as `<account>.json`) inside the `profiles/` folder.

### Method C: Configure Manually in the Script
1. Access [Google Gemini](https://gemini.google.com/) logged in.
2. Copy the values of `__Secure-1PSID` and `__Secure-1PSIDTS` cookies from the Browser Inspector (`F12`).
3. Open the [proxy_server.py](file:///e:/1CODE/Gemini-API/proxy_server.py) file and set them in the `SECURE_1PSID` and `SECURE_1PSIDTS` variables at the top of the file.

---

## Step 2: Run the Proxy Server on Windows (With Model Menu)

With cookies configured:

1. Open **Command Prompt (CMD)** or **PowerShell** in the root of the project folder (`e:\1CODE\Gemini-API`).
2. Run the batch script by typing:
   ```cmd
   setup_and_run.bat
   ```
3. The script will create the virtual environment (`venv`), install dependencies, and run `select_model.py` interactively.
4. **Interactive Menu**: The terminal will display all available models in your account fetched directly from the Gemini server. Choose the corresponding option by typing its number. Your choice will be recorded locally in `.selected_model` for the server to use.
5. Next, the local FastAPI server will start at the address `http://localhost:8000`.

---

## Step 3: Configure Endpoints in Third-Party Apps

In the connections/AI settings panel of your client application, insert the following exact `curl` commands in the Custom Provider settings:

### 1. Curl Command for Custom LLM Provider (OpenAI Compatible)
Insert this exact command to add the language and vision provider:

```bash
curl "http://localhost:8000/v1beta/openai/chat/completions" \
  -H "Authorization: Bearer {{API_KEY}}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "{{MODEL}}",
    "messages": [{"role": "system", "content": "{{SYSTEM_PROMPT}}"}, {"role": "user", "content": [{"type": "text", "text": "{{TEXT}}"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,{{IMAGE}}"}}]}]
  }'
```

* **Note**: Replace the `{{...}}` tags if your application requires another native tag format. The local proxy takes care of reading the system prompt, user text, and any base64 image (`{{IMAGE}}`), sending everything in an integrated way to Gemini.

---

### 2. Curl Command for Custom Speech-to-Text (STT) Provider
Insert this exact command to add the voice transcription provider:

```bash
curl -X POST "http://localhost:8000/v1/speech:recognize?key=YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "encoding": "LINEAR16",
      "sampleRateHertz": 16000,
      "languageCode": "pt-BR",
      "alternativeLanguageCodes": ["en-US"]
    },
    "audio": {
      "content": "{{AUDIO}}"
    }
  }'
```

* **Note**: The proxy will intercept this command, decode the `LINEAR16` (PCM) audio contained in `{{AUDIO}}`, automatically convert it to a temporary `.wav` file, and use Gemini's multimodal understanding to quickly extract and transcribe the spoken message.

---

## Troubleshooting (FAQ)

### 1. Invalid Cookie Error or "Communication failure" in the wrapper
* **Reason:** Google Gemini cookies expire periodically or the session was revoked by Google.
* **Solution:** Thanks to the proxy's self-healing, the server attempts to renew cookies on its own if you used **Method A** (Playwright). If the login expires completely, simply run the visual login again in the terminal:
  ```bash
  python sync_cookies.py <account_name> --login
  ```
  Log in and close the window. The proxy will resume working immediately.

### 2. Port 8000 is already in use
* **Solution:** You can open the `setup_and_run.bat` file in Notepad and change the default port at the end of the file from `--port 8000` to another free port (e.g. `--port 8080`), also adjusting the URLs configured in the destination applications.

### 3. Permission Error when running scripts
* **Solution:** If using PowerShell and you receive an execution policy error, make sure to run it in the classic Command Prompt (`cmd.exe`) or configure the temporary execution policy in PowerShell by running: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` before calling the script.
