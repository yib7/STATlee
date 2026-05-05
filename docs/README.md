# CodeCaster

**An AI-Assisted Data Analysis Platform for Social Scientists**

CodeCaster is a web-based, AI-driven data analysis platform that abstracts away the coding process. Designed primarily for social science students and researchers, it allows users to upload datasets and use natural language to request complex analytical workflows without needing to write Python or R syntax.

Built using a multi-model Google Gemini architecture, CodeCaster generates, validates, and securely executes code in a local sandboxed environment, providing statistical interpretations and visualizations natively.

##  Key Features

* **Intelligent Codebook:** Automatically samples uploaded CSVs to classify variables as Nominal, Ordinal, or Continuous, preventing statistical errors (e.g., running linear regression on nominal data).
* **Multi-Model Architecture:** Routes tasks to the most efficient Gemini models (Gemini 3.1 Pro for deep analysis, Gemini 3 Flash for validation/formatting, and Gemini 3.1 Flash Lite for conversational chat and safety moderation).
* **Sandboxed Execution:** Executes generated Python or R scripts securely within an isolated Docker container.
* **AI Interpretation:** Translates dense terminal outputs and p-values into plain-English, Markdown-formatted insights.
* **Conversational Follow-up:** A dedicated "Converse" tab to ask follow-up questions about your specific data and results.

##  Getting Started

You can run CodeCaster on your local machine. Because CodeCaster executes dynamically generated code, running it via Docker is highly recommended to ensure a secure, isolated sandbox environment.

### Prerequisites

* Docker Desktop installed and running.
* A Google Gemini API Key.

### 1. Clone the Repository

```bash
git clone [https://github.com/yourusername/codecaster.git](https://github.com/yourusername/codecaster.git)
cd codecaster
```

### 2. Set Up Environment Variables

Create a `.env` file in the root directory of the project and add your Gemini API key. You can also optionally add a master password to lock the application.

```env
# Required: Your Google Gemini API Key
GEMINI_API_KEY=your_api_key_here

# Optional: Set a password to lock the web interface
PASSWORD=your_secure_password
```

### 3. Build and Run (Using Docker)

Run the following command to build the image and start the container:

```bash
docker-compose up --build
```

Once the container is running, open your browser and navigate to:
 **http://localhost:5000**

---

##  Alternative: Local Developer Setup (Without Docker)

If you prefer to run the Flask application directly on your host machine (*not recommended for untrusted code execution*):

1. Ensure **Python 3.10+** and **R** are installed on your system.
2. Create a virtual environment:

```bash
python -m venv venv
source venv/bin/activate  # On Windows use: venv\Scripts\activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Run the Flask app:

```bash
flask run --host=0.0.0.0 --port=5000
```

##  Tech Stack

* **Frontend:** Vanilla JavaScript, HTML5, Tailwind CSS (via CDN)
* **Backend:** Python, Flask, Pandas, Subprocess
* **AI Integration:** Google GenAI SDK (`gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite-preview`)
* **Infrastructure:** Docker, Docker Compose

##  Website

If you want to try out CodeCaster without dealing with the local setup, head on over to the deployed web version at: https://codecaster-th8m.onrender.com/
```
