# CodeCaster: AI Collaboration Context & System Instructions

## 1. Project Overview
CodeCaster is a proactive, containerized data analysis platform designed to bridge the gap between complex statistical concepts and programming syntax. Its primary demographic includes college students, researchers, and industry analysts in the social sciences who need to generate meaningful analytical observations without deep software engineering expertise or just want to be more productive in an all encompassing environment. The system takes natural language prompts, securely processes CSV uploads, and outputs/executes optimized Python or R scripts.

## 2. Technical Stack Standards
When generating or modifying code for this project, adhere to the following stack and constraints:
* **Backend:** Python 3.12 (slim) with Flask.
* **Data Processing:** `pandas`, `statsmodels` for Python. Base R, `dplyr`, `ggplot2`, and `MASS` for R.
* **Frontend:** HTML5, Vanilla JavaScript, and Tailwind CSS (via CDN).
* **AI Integration:** Google GenAI SDK (`google-genai`) using a multi-agent pipeline using the appropriate model depending on the scale of a task.
* **Infrastructure:** Dockerized environment for secure, local code execution. 

## 3. Architecture & Data Flow Rules
* **File Handling:** Datasets are uploaded to a temporary folder (`tempfile.mkdtemp()`) with a 16 MB limit. The system must automatically clean up files older than 2 hours.
* **Split-Screen UI:** The frontend must maintain a dynamic dashboard with a left sidebar for configuration (upload, codebook, prompt) and a right content area featuring tabs for Source Code, Data Viewer, Analysis Results, and Converse.
* **Execution Sandbox:** Generated scripts must execute locally within the container via `subprocess.run` with a strict 60-second timeout. 
* **Visualization Protocol:** Interactive plot windows are strictly forbidden. All generated plots must be saved locally as `plot.png` (using `plt.savefig()` or `ggsave()`) so the backend can encode them as Base64 for frontend rendering.

## 4. Coding Style Guidelines
* **Frontend:** Prioritize glass-morphism UI designs, Tailwind utility classes, and custom keyframe animations (`fade-in-up`, `blob`) for a sleek, modern feel.
* **Backend:** Keep Flask routes modular. All AI interactions must have structured exception handling and fallback error messages to prevent server crashes on failed API calls.
* **The "Codebook" Constraint:** When drafting analysis scripts, the AI must strictly reference the dynamically generated `codebook` (mapping variables as Nominal, Ordinal, or Continuous) to ensure statistical validity (e.g., preventing OLS regressions on Nominal data).

---

## 5. Vibe Coding Process & AI Reflection Log
*(This section documents some examples of the interactions between me and the AI model, showcasing how the AI was steered, where it failed, and the resulting takeaways.)*

## Cycle 1: Taming LLM Formatting via Prompt Chaining

* **Initial Prompt:** > "plain English interpretation still making slight messes up add section that uses gemini-3.1-flash lite that basically takes the output and restructures such that it will come out in a nice markdown format"
* **AI Output:** Refactored the `/interpret` route into a two-step pipeline. The first call goes to the Pro model for statistical analysis, followed immediately by a call to the Flash-Lite model with a temperature of `0.0` to clean and format the text.
* **Failure/Correction:** The initial single-model approach failed because the larger Pro model was too "creative" with its formatting, resulting in broken Markdown that rendered poorly on the frontend.
* **Takeaway:** Separating the "thinking" agent from the "formatting" agent is highly effective to ensure quality yet structural outputs.

---

## Cycle 2: The Phantom AI Connection Error (Silent JS Crash)

* **Initial Prompt:** > "keep getting Plain-English Interpretation - Failed to connect to AI for interpretation all of a sudden"
* **AI Output:** Traced the error back to the frontend `index.html` file, specifically identifying a missing CDN link for the Markdown parser.
* **Failure/Correction:** The user interface displayed an AI network failure, but the actual issue was a missing JavaScript library (`marked.js`). The code hit a `ReferenceError` when trying to parse the AI's markdown, which triggered the `catch(err)` block, falsely blaming the AI connection. This was fixed by adding the missing `<script>` tag to the `<head>`.
* **Takeaway:** In AI-assisted development, standard programming bugs (like missing dependencies) can easily masquerade as LLM hallucinations or API timeout errors if the frontend error handling is too broad.

---

## Cycle 3: The UI Race Condition

* **Initial Prompt:** > "also if you click 'generate analysis' before the dataset variables are profiled, does it wait? because we need that information"
* **AI Output:** Implemented frontend UI locks, temporarily disabling the "Generate" button and text input during the background Codebook fetch.
* **Failure/Correction:** Because `fetch()` is asynchronous, a fast typist could trigger the code-generation AI before the Codebook was completely built. This resulted in sending an empty JSON object as the constraints, leading to hallucinatory code from the AI.
* **Takeaway:** When chaining AI prompts where Prompt B relies on the background output of Prompt A, you must explicitly enforce sequential locking in the user interface to prevent data race conditions.