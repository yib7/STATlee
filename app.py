import os
import tempfile
import json
import time
import re
import subprocess
import math
import base64
import logging
import pandas as pd
from fpdf import FPDF
import PyPDF2
from flask import Flask, request, jsonify, render_template, session, Response, stream_with_context
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from google import genai
from google.genai import types


# ---------------------------------------------------------------------------
# INITIALIZATION & SETUP
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('codecaster')

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("No GEMINI_API_KEY set for Flask application")

client = genai.Client(api_key=api_key)

app = Flask(__name__, template_folder='templates')

# Sessions require a stable secret key. Falling back to a random key means
# sessions reset on every restart AND break across multiple workers (each
# worker would sign cookies with a different key), so warn loudly.
secret_key = os.environ.get("FLASK_SECRET_KEY")
if not secret_key:
    secret_key = os.urandom(24)
    logger.warning(
        "FLASK_SECRET_KEY not set — using a random key. Logins will reset on "
        "restart and fail across multiple workers. Set FLASK_SECRET_KEY in production."
    )
app.secret_key = secret_key

APP_PASSWORD = os.environ.get("PASSWORD")
UPLOAD_FOLDER = tempfile.mkdtemp()
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 16 MB max upload

# ---------------------------------------------------------------------------
# GEMINI MODEL ROUTING
# ---------------------------------------------------------------------------
# One place to swap models as they graduate from preview. Tasks are routed to
# the cheapest model that can do the job well (see README "Multi-Model").
MODEL_PRO = 'gemini-3.1-pro-preview'                 # deep drafting & conversation
MODEL_FLASH = 'gemini-3-flash-preview'               # classification, suggestions, interpretation
MODEL_FLASH_LITE = 'gemini-3.1-flash-lite-preview'   # moderation, validation, formatting


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def resolve_dataset_path(filename):
    """Map a client-supplied filename to a safe path inside UPLOAD_FOLDER.

    Returns the absolute path, or None for empty names or path-traversal
    attempts (e.g. '../../etc/passwd'). Every endpoint that opens a file by a
    name received from the client MUST go through this.
    """
    if not filename:
        return None
    safe_name = secure_filename(filename)
    if not safe_name:
        return None
    upload_dir = os.path.realpath(app.config['UPLOAD_FOLDER'])
    full_path = os.path.realpath(os.path.join(upload_dir, safe_name))
    # Defense in depth: ensure the resolved path stays within the upload dir.
    if os.path.commonpath([full_path, upload_dir]) != upload_dir:
        return None
    return full_path


def sse_stream(generator):
    """Wrap a generator function in a standard Server-Sent Events response."""
    return Response(
        stream_with_context(generator()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.before_request
def require_auth():
    """Protects all API routes if a password is set in the environment."""
    if not APP_PASSWORD:
        return # No password set, allow all traffic
    
    # Allow traffic to the frontend loader, static files, and login handlers
    if request.endpoint in ['index', 'static', 'check_auth', 'login', 'health_check']:
        return
        
    if not session.get('authenticated'):
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401

@app.route('/check_auth', methods=['GET'])
def check_auth():
    """Tells the frontend if it needs to show the login screen."""
    if not APP_PASSWORD or session.get('authenticated'):
        return jsonify({'status': 'authorized'}), 200
    return jsonify({'error': 'unauthorized'}), 401

@app.route('/login', methods=['POST'])
def login():
    """Validates the password."""
    if not APP_PASSWORD:
        return jsonify({'status': 'success'}), 200
        
    data = request.json
    if data.get('password') == APP_PASSWORD:
        session['authenticated'] = True
        return jsonify({'status': 'success'}), 200
        
    return jsonify({'error': 'Invalid password'}), 401

@app.route('/')
def index():
    return render_template('index.html')


def cleanup_old_files():
    """Deletes datasets older than 2 hours to save server space."""
    now = time.time()
    folder = app.config['UPLOAD_FOLDER']
    
    for filename in os.listdir(folder):
        filepath = os.path.join(folder, filename)
        # Check if it's a file and if it's older than 7200 seconds (2 hours)
        if os.path.isfile(filepath) and os.stat(filepath).st_mtime < now - 7200:
            try:
                os.remove(filepath)
                logger.info("Cleaned up old file: %s", filename)
            except OSError:
                pass

@app.route('/upload', methods=['POST'])
def upload_file():
    """Handles dataset uploads and generates a basic profile."""

    cleanup_old_files()

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if file and file.filename.endswith('.csv'):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        try:
            df = pd.read_csv(filepath, nrows=100)
            profile = {
                'total_columns': len(df.columns),
                'numeric_columns': df.select_dtypes(include=['number']).columns.tolist(),
                'categorical_columns': df.select_dtypes(exclude=['number']).columns.tolist(),
                'headers': df.columns.tolist()
            }
            return jsonify({'message': 'File uploaded', 'filename': filename, 'profile': profile}), 200
        except Exception as e:
            return jsonify({'error': f"Error reading CSV: {str(e)}"}), 500
            
    return jsonify({'error': 'Invalid file format. Please upload a CSV.'}), 400

@app.route('/upload_pdf', methods=['POST'])
def upload_pdf():
    """Handles optional PDF or TXT codebook uploads. Max 50 pages."""
    cleanup_old_files()

    MAX_PAGES = 50

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    # Accept both PDF and TXT
    if file and (file.filename.lower().endswith('.pdf') or file.filename.lower().endswith('.txt')):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        # Enforce 50-page limit on PDFs before any further processing
        if filename.lower().endswith('.pdf'):
            try:
                with open(filepath, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    page_count = len(reader.pages)
                if page_count > MAX_PAGES:
                    os.remove(filepath)
                    return jsonify({
                        'error': f'PDF is {page_count} pages, which exceeds the {MAX_PAGES}-page limit. Please upload a shorter document.'
                    }), 400
            except Exception as e:
                os.remove(filepath)
                return jsonify({'error': f'Failed to read PDF: {str(e)}'}), 500

        # Convert TXT to PDF on the fly, then enforce page limit on the result
        if filename.lower().endswith('.txt'):
            pdf_filename = filename.rsplit('.', 1)[0] + '.pdf'
            pdf_filepath = os.path.join(app.config['UPLOAD_FOLDER'], pdf_filename)
            
            try:
                pdf = FPDF()
                pdf.set_auto_page_break(auto=True, margin=15)
                pdf.add_page()
                pdf.set_font("Arial", size=11)
                
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        # FPDF built-in fonts use latin-1 encoding
                        clean_line = line.encode('latin-1', 'ignore').decode('latin-1')
                        pdf.multi_cell(0, 6, txt=clean_line)
                
                pdf.output(pdf_filepath)
                os.remove(filepath)  # Remove original txt

                # Check resulting PDF page count
                with open(pdf_filepath, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    page_count = len(reader.pages)
                if page_count > MAX_PAGES:
                    os.remove(pdf_filepath)
                    return jsonify({
                        'error': f'TXT file converts to {page_count} pages, which exceeds the {MAX_PAGES}-page limit. Please upload a shorter document.'
                    }), 400

                filename = pdf_filename  # Update filename to the new PDF
                
            except Exception as e:
                return jsonify({'error': f'Failed to convert TXT to PDF: {str(e)}'}), 500

        return jsonify({'message': 'Documentation uploaded', 'filename': filename}), 200
        
    return jsonify({'error': 'Invalid file format. Please upload a PDF or TXT.'}), 400


@app.route('/extract_pdf_codebook', methods=['POST'])
def extract_pdf_codebook():
    """Sends the PDF inline to Gemini as bytes to map variable labels."""
    data = request.json
    filename = data.get('filename')

    if not filename:
        return jsonify({'error': 'Missing filename'}), 400

    filepath = resolve_dataset_path(filename)
    if not filepath:
        return jsonify({'error': 'Invalid filename'}), 400

    try:
        with open(filepath, 'rb') as f:
            pdf_bytes = f.read()

        prompt = """
        You are a data dictionary extractor for a social science tool.
        Read the attached PDF codebook.
        Identify all variable/column names and their corresponding descriptions, labels, or coding schemes (e.g., what 1=, 2= means).
        Return STRICTLY a JSON object mapping the variable names (as keys) to their descriptive strings (as values).
        Ensure it is valid JSON and contains NO markdown formatting outside the JSON itself.
        Example: {"pid7": "7-point Party Identification (1=Strong Democrat, 7=Strong Republican)", "income": "Household income bracket"}
        """

        response = client.models.generate_content(
            model=MODEL_FLASH,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type='application/pdf'),
                prompt
            ],
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json"
            )
        )
        return jsonify({'status': 'success', 'mapping': json.loads(response.text)})
    except Exception as e:
        logger.error("PDF extraction error: %s", e)
        return jsonify({'error': str(e)}), 500

@app.route('/data_page', methods=['POST'])
def data_page():
    """Returns a specific 100-row page of the dataset with optional column filters applied."""
    data = request.json
    filename = data.get('filename')
    page = int(data.get('page', 1))
    per_page = int(data.get('per_page', 100))
    filters = data.get('filters', {}) 

    if not filename:
        return jsonify({'error': 'Missing filename'}), 400

    filepath = resolve_dataset_path(filename)
    if not filepath:
        return jsonify({'error': 'Invalid filename'}), 400

    try:
        df = pd.read_csv(filepath)

        for col, search_term in filters.items():
            if search_term and col in df.columns:
                df = df[df[col].astype(str).str.contains(str(search_term), case=False, na=False)]
        
        total_rows = len(df)
        total_pages = math.ceil(total_rows / per_page)
        
        if page < 1: page = 1
        if page > total_pages and total_pages > 0: page = total_pages
        
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        
        df_page = df.iloc[start_idx:end_idx].fillna("")
        
        return jsonify({
            'status': 'success',
            'data': df_page.to_dict(orient='records'),
            'total_rows': total_rows,
            'total_pages': total_pages,
            'current_page': page
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/classify_variables', methods=['POST'])
def classify_variables():
    """FEATURE: The Codebook - Intelligently classifies variables."""
    data = request.json
    filename = data.get('filename')
    
    if not filename: 
        return jsonify({'error': 'Missing filename'}), 400
    
    filepath = resolve_dataset_path(filename)
    if not filepath:
        return jsonify({'error': 'Invalid filename'}), 400

    try:
        # 1. Read a safe, fast sample
        df = pd.read_csv(filepath, nrows=100)
        
        # 2. Create a compact, highly-readable JSON summary for the AI
        # This prevents Pandas from truncating wide datasets with '...' 
        # and drastically reduces token count for faster generation.
        summary = {}
        for col in df.columns:
            # Grab up to 3 unique, non-null samples
            samples = df[col].dropna().astype(str).unique()[:3].tolist()
            summary[col] = {
                "type": str(df[col].dtype),
                "samples": samples
            }
            
        data_preview = json.dumps(summary, indent=2)
        
        prompt = f"""
        Analyze this dataset summary below. Your goal is to Classify EVERY column listed in the summary as 'Nominal', 'Ordinal', or 'Continuous'. 
        Return STRICTLY a JSON object mapping column names to their classification.
        Example: {{"Age": "Continuous", "Gender": "Nominal", "Education_Level": "Ordinal"}}
        
        DATA SUMMARY:
        {data_preview}
        """
        response = client.models.generate_content(
            model=MODEL_FLASH,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1, 
                response_mime_type="application/json"
            )
        )
        return jsonify({'status': 'success', 'codebook': json.loads(response.text)})
    except Exception as e:
        logger.error("Classification error: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/suggest', methods=['POST'])
def suggest_analysis():
    """Generates proactive analytical questions based on the uploaded data."""
    data = request.json
    filename   = data.get('filename')
    codebook   = data.get('codebook', {})       # {col: 'Continuous'|'Nominal'|'Ordinal'}
    pdf_mapping = data.get('pdf_mapping', {})   # {col: 'human-readable description from PDF'}

    if not filename:
        return jsonify({'error': 'Missing filename'}), 400

    filepath = resolve_dataset_path(filename)
    if not filepath:
        return jsonify({'error': 'Invalid filename'}), 400

    try:
        df = pd.read_csv(filepath, nrows=100)

        # Build a rich per-column context block that layers:
        #   1. Intelligent codebook classification (Continuous / Nominal / Ordinal)
        #   2. PDF-parsed description, if the researcher uploaded a codebook
        #   3. dtype + sample values as a baseline fallback
        # Case-insensitive lookup for pdf_mapping so minor capitalisation
        # differences between the PDF and the CSV header don't cause misses.
        pdf_lower = {k.lower(): v for k, v in pdf_mapping.items()}

        column_context_lines = []
        for col in df.columns:
            classification = codebook.get(col, 'Unknown')
            description    = pdf_lower.get(col.lower(), '')
            samples        = df[col].dropna().astype(str).unique()[:3].tolist()
            dtype          = str(df[col].dtype)

            line = f"  - '{col}' [{classification}, dtype={dtype}, samples={samples}]"
            if description:
                line += f"\n      Codebook description: {description}"
            column_context_lines.append(line)

        column_context = "\n".join(column_context_lines)

        prompt = f"""
        You are a proactive social science data mentor helping users get the most out of CodeCaster,
        an AI analysis tool. Below is a rich description of every column in the uploaded dataset,
        including its measurement level (Continuous / Nominal / Ordinal), data type, sample values,
        and — where available — a human-readable description extracted from the researcher's codebook.

        DATASET COLUMN DETAILS:
        {column_context}

        Using this information, suggest 3 ready-to-use analysis prompts that a researcher could paste
        directly into CodeCaster.

        RULES FOR EACH SUGGESTION — every suggestion MUST:
        1. Reference specific column names using their EXACT labels as shown above.
        2. Choose statistically appropriate methods for the measurement levels involved
        3. Specify at least one concrete visualization (e.g., scatter plot with regression line,
        grouped bar chart, correlation heatmap, box plot by group, histogram, etc.).
        4. Be written as a direct, actionable instruction — start with an imperative verb
        (e.g., "Run...", "Generate...", "Conduct...", "Plot...").
        5. Be 1–2 sentences, specific enough that a coding assistant can act on it immediately.
        6. Where codebook descriptions are available, use proper scale names and value labels from
        those descriptions to make the suggestion contextually meaningful.

        GOOD EXAMPLE (format guide only — do not repeat):
        "Run an OLS regression predicting 'life_satisfaction' (Continuous) from 'income' (Continuous),
        'education_level' (Ordinal), and 'party_id' (Nominal); include a residual scatter plot and a
        correlation heatmap of all predictors."

        BAD EXAMPLES (never produce):
        - "Explore the relationship between variables." (vague, no column names, no method, no plot)
        - "What patterns exist in this data?" (a question, not an instruction)

        Return STRICTLY a JSON array of exactly 3 strings and nothing else.
        Example format: ["Suggestion 1", "Suggestion 2", "Suggestion 3"]
"""
        response = client.models.generate_content(
            model=MODEL_FLASH,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.7,
                response_mime_type="application/json"
            )
        )
        return jsonify({'status': 'success', 'suggestions': json.loads(response.text)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/chat', methods=['POST'])
def chat():
    """
    CORE PIPELINE:
    1. Moderation Check (Is it safe/relevant?)
    2. Feature Selection (Only on wide datasets — picks the strictly necessary
       columns so the Pro draft model isn't bloated with irrelevant schema.
       Skipped when the dataset has fewer than 15 columns.)
    3. Code Generation (Drafting against the user's original prompt + the
       filtered codebook)
    4. Code Validation (Reviewing the draft for execution errors — streamed)
    """
    data = request.json
    user_prompt = data.get('prompt')
    filename = data.get('filename')
    target_language = data.get('language', 'Python')
    history = data.get('history', [])
    codebook = data.get('codebook', {})

    if not user_prompt or not filename: 
        return jsonify({'error': 'Missing prompt or filename'}), 400

    moderation_prompt = f"""
    You are a strict safety and relevance filter for an academic data analysis tool.
    Assess the following user request: "{user_prompt}"
    
    Rules:
    1. If the request is asking to build malware, hack, or engage in illegal/unethical acts, reply STRICTLY with "BLOCK: Safety Violation".
    2. If the request is wildly off-topic (e.g., "write a poem about cats", "how do I bake a cake"), reply STRICTLY with "BLOCK: Off-topic".
    3. If the request is a valid data analysis, coding, or statistical query, reply STRICTLY with "PASS".
    """
    try:
        mod_response = client.models.generate_content(
            model=MODEL_FLASH_LITE, 
            contents=moderation_prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )
        mod_result = mod_response.text.strip()
        
        if "BLOCK" in mod_result:
            return jsonify({'error': f"Request denied. {mod_result}"}), 403
            
    except Exception as e:
        return jsonify({'error': 'Moderation service failed.'}), 500

    filepath = resolve_dataset_path(filename)
    if not filepath:
        return jsonify({'error': 'Invalid filename'}), 400
    try:
        df = pd.read_csv(filepath, nrows=0)
        headers = df.columns.tolist()
    except Exception as e: 
        return jsonify({'error': 'Could not read dataset headers.'}), 500

    # ---------------------------------------------------------------------
    # STAGE 1: Feature Selection (wide-dataset gate)
    # ---------------------------------------------------------------------
    # On wide datasets, the full header list and codebook bloat the prompt
    # sent to the Pro draft model with columns the analysis won't even touch.
    # A cheap Flash call picks the strictly necessary subset; the Pro model
    # then sees a focused schema.
    #
    # We deliberately do NOT rewrite the user's prompt — the Pro model is
    # already good at interpreting natural language, and rewriting risks
    # erasing intent (e.g., flattening an ambiguous request into the wrong
    # statistical test based on the cheaper model's guess).
    #
    # The gate (>= 15 columns) skips this layer on typical small social
    # science datasets where the savings don't justify the extra LLM call.
    # ---------------------------------------------------------------------

    FEATURE_SELECTION_THRESHOLD = 15

    # Defaults — used when the gate skips Stage 1, or if Stage 1 fails /
    # returns garbage. Either way the endpoint degrades to its previous
    # behavior rather than erroring out.
    filtered_headers = headers
    filtered_codebook = codebook

    if len(headers) >= FEATURE_SELECTION_THRESHOLD:
        # Prefer the codebook as column context if available — its type
        # classifications help the selector pick appropriate variables.
        if codebook:
            column_context = json.dumps(codebook, indent=2)
        else:
            column_context = ", ".join(headers)

        feature_selection_prompt = f"""
        You are a feature selection engine for a social science data analysis tool. A user has made an analysis request against a dataset with many columns. Your job is to identify the STRICT MINIMUM set of column names required to fulfill the request — nothing more.

        DATASET COLUMNS (with classifications where available):
        {column_context}

        USER REQUEST:
        "{user_prompt}"

        Rules:
        - Use only column names that appear in the dataset above (exact match, case-sensitive).
        - Do not include columns that aren't directly used by the analysis.
        - If the request is exploratory or descriptive (e.g., "summarize this data", "what's interesting here"), err on the side of INCLUDING more columns rather than fewer.

        Return STRICTLY a JSON object with this exact shape (no markdown, no commentary):
        {{
        "required_columns": ["<col1>", "<col2>", "..."]
        }}
        """
        try:
            sel_response = client.models.generate_content(
                model=MODEL_FLASH,
                contents=feature_selection_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json"
                )
            )
            sel_result = json.loads(sel_response.text)
            candidate_cols = sel_result.get('required_columns', [])

            # Validate against actual headers; drop hallucinations. If
            # nothing validates, keep the full header list rather than
            # handing the draft model an empty schema.
            if isinstance(candidate_cols, list):
                validated = [c for c in candidate_cols if c in headers]
                if validated:
                    filtered_headers = validated
                    if codebook:
                        filtered_codebook = {c: codebook[c] for c in validated if c in codebook}

            logger.info("[/chat] Stage 1 selected %d/%d columns: %s",
                        len(filtered_headers), len(headers), filtered_headers)
        except Exception as e:
            logger.warning("[/chat] Stage 1 (feature selection) failed, falling back to full schema: %s", e)
            # Defaults already set above; analysis proceeds normally.
    else:
        logger.info("[/chat] Stage 1 skipped: %d columns is below threshold (%d).",
                    len(headers), FEATURE_SELECTION_THRESHOLD)

    # ---------------------------------------------------------------------
    # STAGE 2: Code Generation (Drafting)
    # Uses the user's original prompt (intent-preserving) with a schema
    # that's been filtered down on wide datasets.
    # ---------------------------------------------------------------------
    system_instruction = f"""
    You are CodeCaster, an expert social science data analyst. 
    Dataset: '{filename}'. Columns: {', '.join(filtered_headers)}.
    
    INTELLIGENT CODEBOOK CLASSIFICATION:
    {json.dumps(filtered_codebook)}
    *CRITICAL*: Use this codebook to ensure statistical validity. Do not run continuous models (like OLS) on Nominal data. Select the appropriate tests based on variable type.
    
    CONSTRAINTS:
    1. Output ONLY raw, executable {target_language} code. No markdown formatting (no ```).
    2. No explanations in the output.
    3. If you generate a plot or graph, you MUST save it locally as 'plot.png'. NEVER open interactive plot windows.
    """

    if target_language.lower() == 'python':
        system_instruction += "\nUse pandas, statsmodels, matplotlib/seaborn. print() important statistical summaries to stdout. Use plt.savefig('plot.png', bbox_inches='tight') for plots."
    else:
        system_instruction += "\nUse base R, dplyr, ggplot2. print() or summary() models to stdout. Use ggsave('plot.png') or standard png('plot.png') for plots."

    draft_prompt = system_instruction + "\n\n--- CONVERSATION HISTORY ---\n"
    for msg in history: 
        draft_prompt += f"{msg['role'].upper()}: {msg['text']}\n"
    draft_prompt += f"\n--- CURRENT REQUEST ---\n{user_prompt}\nDRAFT SCRIPT:"

    try:
        draft_response = client.models.generate_content(
            model=MODEL_PRO,
            contents=draft_prompt,
            config=types.GenerateContentConfig(temperature=0.1)
        )
        draft_code = draft_response.text.strip()
    except Exception as e: 
        return jsonify({'error': 'Failed to generate code draft.'}), 500

    validation_prompt = f"""
    You are a senior code reviewer. Review the following {target_language} script designed to analyze a dataset named '{filename}'.
    
    DRAFT CODE:
    {draft_code}
    
    TASKS:
    1. Ensure all necessary libraries are imported (e.g., pandas, statsmodels in Python).
    2. Ensure missing values (NaN/NA) are handled before running statistical models.
    3. Ensure the code will execute without throwing syntax or TypeErrors.
    4. Output ONLY the finalized, raw executable code. NO MARKDOWN (do not use ```). NO EXPLANATIONS.
    """

    # Stream the validation pass to the client via Server-Sent Events.
    # Pre-flight steps (moderation, draft generation) above remain synchronous
    # because they are gates / inputs to this final pass; only the user-facing
    # text is streamed.
    def generate():
        accumulated = ""
        try:
            stream = client.models.generate_content_stream(
                model=MODEL_FLASH_LITE,
                contents=validation_prompt,
                config=types.GenerateContentConfig(temperature=0.0)
            )
            for chunk in stream:
                # Some chunks may have no text (e.g. usage metadata only)
                delta = getattr(chunk, 'text', None)
                if delta:
                    accumulated += delta
                    yield f"data: {json.dumps({'type': 'delta', 'text': delta})}\n\n"

            # Post-stream: run the same defensive fence-stripping as before so
            # `currentCode` on the client ends up clean even if the model
            # ignored the no-markdown instruction.
            final_code = accumulated.strip()
            match = re.search(r'```(?:python|r)?\n(.*?)```', final_code, re.DOTALL | re.IGNORECASE)
            if match:
                final_code = match.group(1).strip()
            elif final_code.startswith("```"):
                final_code = '\n'.join(
                    line for line in final_code.split('\n')
                    if not line.strip().startswith('```')
                )

            yield f"data: {json.dumps({'type': 'done', 'code': final_code, 'language': target_language})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'Code validation failed: {str(e)}'})}\n\n"

    return sse_stream(generate)


@app.route('/run', methods=['POST'])
def run_code():
    """Executes the generated code locally ONLY."""
    data = request.json
    code = data.get('code')
    language = (data.get('language') or 'Python')

    if not code:
        return jsonify({'error': 'No code provided.'}), 400

    is_python = language.lower() == 'python'

    try:
        suffix = '.py' if is_python else '.R'
        fd, path = tempfile.mkstemp(suffix=suffix, dir=app.config['UPLOAD_FOLDER'])
        
        with os.fdopen(fd, 'w') as f: 
            f.write(code)
            
        plot_file = os.path.join(app.config['UPLOAD_FOLDER'], 'plot.png')
        if os.path.exists(plot_file):
            os.remove(plot_file)
        
        cmd = ['python', path] if is_python else ['Rscript', path]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=app.config['UPLOAD_FOLDER'],
            timeout=60
        )
        
        final_output = result.stdout
        if result.stderr: 
            final_output += f"\n--- Output/Warnings ---\n{result.stderr}"
            
        os.remove(path)
        
        plot_data = None
        if os.path.exists(plot_file):
            with open(plot_file, "rb") as image_file:
                plot_data = base64.b64encode(image_file.read()).decode('utf-8')
            os.remove(plot_file) 

        return jsonify({'output': final_output.strip(), 'plot': plot_data})

    except subprocess.TimeoutExpired: 
        return jsonify({'error': 'Execution timed out. Code took too long to run.'}), 500
    except Exception as e: 
        return jsonify({'error': str(e)}), 500


@app.route('/interpret', methods=['POST'])
def interpret_results():
    """Handles the AI interpretation step with a two-step generation and formatting pipeline.
    The final formatting pass is streamed to the client via SSE."""
    data = request.json
    final_output = data.get('output', '')
    plot_base64 = data.get('plot') # Retrieve the plot image data

    # If there's nothing to interpret, return a plain JSON response (preserves
    # the original behavior — client handles this as a non-streamed case).
    if not final_output.strip() and not plot_base64:
        return jsonify({'interpretation': "No statistical output or plots generated to interpret."})

    # STEP 1: Generate the deep analysis using the Pro model (multimodal).
    # This stays synchronous because its output feeds the formatting pass.
    interp_prompt = f"""
    You are a data mentor. A user ran an analysis script and got the following terminal results.
    
    TERMINAL OUTPUT:
    {final_output if final_output.strip() else "No terminal text output."}
    """
    
    if plot_base64:
        interp_prompt += "\n\nYou have also been provided with the generated plot. Please analyze the visual trends, distributions, or relationships shown in the graph in conjunction with the terminal output."

    interp_prompt += """
    
    Write a clear, plain-English summary of what this means for a social scientist. 
    Interpret p-values, correlations, trends, or any findings from these terminal results 
    and the provided graph (if any) that are relevant for a data analyst/social scientist to know. 
    Do not introduce yourself, go straight into summarizing and analyzing.
    """
    
    contents_payload = [interp_prompt]
    if plot_base64:
        try:
            image_bytes = base64.b64decode(plot_base64)
            contents_payload.append(
                types.Part.from_bytes(data=image_bytes, mime_type='image/png')
            )
        except Exception as e:
            logger.error("Error decoding image for AI: %s", e)
    
    try:
        ai_resp = client.models.generate_content(
            model=MODEL_FLASH,
            contents=contents_payload,
            config=types.GenerateContentConfig(temperature=0.3)
        )
        raw_interpretation = ai_resp.text.strip()
    except Exception as e:
        logger.error("Interpretation error: %s", e)
        return jsonify({'interpretation': "Failed to generate AI interpretation."}), 500

    # STEP 2: Strict Markdown formatting pass using Flash — STREAMED to client.
    format_prompt = f"""
    You are a strict text formatter. Take the following statistical interpretation and format it into beautiful, perfectly clean Markdown.
    
    CRITICAL FORMATTING RULES:
    1. Use standard Markdown headers (###) for distinct sections.
    2. Use simple bullet points (*) for lists and key findings.
    3. Fix any broken formatting, orphaned asterisks, or messy spacing.
    4. DO NOT change the content of the text unless there is an emoji or emoticon detected, then delete it.
    5. Include proper spacing when necessary to improve readability and prevent users from being overwhelmed. 
    6. Output ONLY the formatted Markdown.

    RAW INTERPRETATION:
    {raw_interpretation}
    """

    def generate():
        try:
            stream = client.models.generate_content_stream(
                model=MODEL_FLASH_LITE,
                contents=format_prompt,
                config=types.GenerateContentConfig(temperature=0.0)
            )
            for chunk in stream:
                delta = getattr(chunk, 'text', None)
                if delta:
                    yield f"data: {json.dumps({'type': 'delta', 'text': delta})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            logger.error("Interpretation format error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Failed to format AI interpretation.'})}\n\n"

    return sse_stream(generate)

@app.route('/health')
def health_check():
    return "OK", 200

@app.route('/converse', methods=['POST'])
def converse():
    """FEATURE: The Converse Tab - Contextual chat with a 3.1 Flash Lite formatting pass.
    The final formatting pass is streamed to the client via SSE."""
    data = request.json
    message = data.get('message')
    history = data.get('history', [])
    context = data.get('context', '') 
    code = data.get('code', '') # Grab the generated code from the frontend

    # STEP 1: Deep Analysis Prompt (Pro Model) — stays synchronous because its
    # output is the input to the streamed formatting pass.
    analysis_prompt = f"""
    You are CodeCaster, an academic mentor.
    The user is asking a question about their recent analysis.
    
    --- RECENT RESULTS CONTEXT ---
    {context}
    
    --- GENERATED SCRIPT ---
    {code if code else "No code generated yet."}
    
    Answer the user's question directly, clearly, and concisely based on their data and the specific code provided.
    
    --- CONVERSATION HISTORY ---
    """
    for msg in history: 
        analysis_prompt += f"{msg['role'].upper()}: {msg['text']}\n"
        
    analysis_prompt += f"\nUSER: {message}\nCODECASTER:"

    try:
        raw_response = client.models.generate_content(
            model=MODEL_PRO,
            contents=analysis_prompt,
            config=types.GenerateContentConfig(temperature=0.6)
        )
        raw_answer = raw_response.text.strip()
    except Exception as e:
        logger.error("Converse error: %s", e)
        return jsonify({'error': str(e)}), 500

    # STEP 2: Strict Formatting Pass (Flash Lite Model) — STREAMED to client.
    format_prompt = f"""
    You are a strict text formatter. Take the following conversational response and format it into clean, simple Markdown.
    
    CRITICAL FORMATTING RULES:
    1. Keep the structure simple and straightforward. Do not over-structure with unnecessary headers.
    2. Use inline code blocks (`like this`) for variable names, functions, or small code snippets.
    3. Use standard code blocks (```) only if providing a multi-line code correction.
    4. Use simple bullet points ONLY if listing multiple distinct items or steps.
    5. DO NOT change the underlying meaning, tone, or advice.
    6. Output ONLY the formatted Markdown.

    RAW RESPONSE:
    {raw_answer}
    """

    def generate():
        try:
            stream = client.models.generate_content_stream(
                model=MODEL_FLASH_LITE,
                contents=format_prompt,
                config=types.GenerateContentConfig(temperature=0.0)
            )
            for chunk in stream:
                delta = getattr(chunk, 'text', None)
                if delta:
                    yield f"data: {json.dumps({'type': 'delta', 'text': delta})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            logger.error("Converse format error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return sse_stream(generate)

if __name__ == '__main__':
    # Grab the port Render wants to use, or default to 5000 locally
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)